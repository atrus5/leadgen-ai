"""
LeadGen AI — Agency Orchestrator

Endpoints:
  POST /run-audit              Legacy inbound form ingest (kept for the landing
                               page on /. Persists to SQLite + sends a Discord
                               webhook if configured.)
  POST /new-lead               Legacy webhook for an inbound reply; persists to
                               `replies`, classifies intent, forwards HOT.

  GET  /                       Marketing landing page (existing public/index.html)
  GET  /wizard                 Setup wizard (config + DNS + warmup plan)
  GET  /playbook               Step-by-step docs landing
  GET  /playbook/<stage>       Each step's long-form guide
  GET  /healthz                Liveness probe
  GET  /api/dashboard/summary  Counts for the dashboard
  GET  /api/clients            List clients
  POST /api/clients            Create client
  PUT  /api/clients/<id>       Update client
  DELETE /api/clients/<id>     Pause client (soft-delete via status)
  GET  /api/clients/<id>/prospects    List prospects for one client
  GET  /api/prospects          List all prospects
  POST /api/prospects          Add prospect manually
  PUT  /api/prospects/<id>     Update prospect
  DELETE /api/prospects/<id>   Delete prospect
  GET  /api/warmups            List warmup domains
  POST /api/warmups            Add domain to warmup
  GET  /api/replies            List replies with filter intent
  POST /api/hunt               Trigger hunters now for one client (or all)
  POST /api/brief              Trigger morning brief now
  POST /api/check-inbox        Pull + classify + forward right now
  POST /api/jobs/run           Run a named job on demand
  POST /api/email/send         Generate + send cold email to one prospect
  POST /api/email/followup     Generate + send a followup step
  GET  /api/email/remaining-quota   Quota for the configured from-address
  GET  /api/niches             Niches metadata (UI fills from this)
  GET  /api/settings           Read all settings
  PUT  /api/settings           Update one settings key

Boot:
  - Initialises SQLite schema on import.
  - Starts APScheduler if scheduler.enabled is True.
  - On Windows / forks, the scheduler is a no-op daemon.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import (
    Flask, Blueprint, jsonify, request, send_from_directory, abort, Response
)

from . import auth, config, db
from .hunters import apollo, google_places
from .outreach import forwarder, generator, reply_parser, sender
from .scheduler import jobs as sched_jobs

import os as _os
import secrets as _secrets

LOG = logging.getLogger("app")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
PLAYBOOK_DIR = PUBLIC_DIR / "playbook"

# Single Flask app
app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")
app.config["JSON_SORT_KEYS"] = False

# ── Session / auth wiring (Task #1) ────────────────────────────────────
_FLASK_SECRET = _os.environ.get("LEADGEN_FLASK_SECRET")
if _FLASK_SECRET:
    app.secret_key = _FLASK_SECRET.encode("utf-8")
else:
    app.secret_key = _os.urandom(32).hex()
    LOG.warning(
        "LEADGEN_FLASK_SECRET env var not set — using a random per-process "
        "session key. Operators will be silently logged out across restarts. "
        "Set LEADGEN_FLASK_SECRET in /etc/leadgen.env for production."
    )
try:
    _base_url = (config.load_settings().get("tracking") or {}).get("base_url") or ""
    app.config["SESSION_COOKIE_SECURE"] = _base_url.startswith("https://")
except Exception:
    app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 12 * 60 * 60

# ── Boot ─────────────────────────────────────────────────────────────────
db.init_schema()
db.ensure_default_settings()
# Persist a sample settings.json so the operator can see what to fill in.
config.write_default_settings_file()
sched_jobs.install()
# First-boot auth + webhook secret bootstrap. Idempotent.
auth.generate_setup_token_if_uninstalled()
auth.ensure_webhook_secret()


# ── Helpers ──────────────────────────────────────────────────────────────
def _get_json() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _err(msg: str, code: int = 400) -> tuple[Any, int]:
    return jsonify({"ok": False, "error": msg}), code


def _ok(payload: Any = None, **extra: Any) -> Any:
    out = {"ok": True}
    if payload is not None:
        out["data"] = payload
    out.update(extra)
    return jsonify(out)


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row else {}


# ── Legacy webhook compatibility ────────────────────────────────────────
def _check_webhook_token():
    """Gate /run-audit and /new-lead. Returns (response, status) tuple if
    blocked, None if allowed. Constant-time compare via secrets.compare_digest."""
    supplied = (request.args.get("token") or "").strip()
    expected = auth._webhook_secret()
    if not expected:
        LOG.error("webhook_secret missing in settings — refusing inbound webhook")
        return _err("webhook_not_initialized", 503)
    if not supplied or not _secrets.compare_digest(supplied, expected):
        return _err("invalid_token", 401)
    return None


@app.route("/run-audit", methods=["POST"])
def run_audit_legacy():
    """The landing page (public/index.html) posts here when a visitor requests a free audit.
    Gated by ?token=<webhook_secret> — see GET /api/auth/webhook/url."""
    gated = _check_webhook_token()
    if gated is not None:
        return gated
    data = _get_json()
    biz_name = (data.get("business_name") or "").strip() or "Unknown"
    email = (data.get("email") or "").strip().lower()
    db.audit("inbound.audit_request", {"business_name": biz_name, "email": email})
    # Placeholder audit score. Single source of truth: the value below is
    # mirrored at both top-level (so JS reading `data.score` doesn't get
    # "undefined") AND nested in `data.data.score` (for callers that walk
    # `data.data.*`). Replace with an Ollama-driven score when the LLM
    # hook is wired up.
    audit_score = "84/100"
    return _ok(
        {"queued": True, "score": audit_score},
        business_name=biz_name,
        score=audit_score,
    )


@app.route("/new-lead", methods=["POST"])
def new_lead_legacy():
    """Webhook used by an external pipeline to push an inbound reply.
    Gated by ?token=<webhook_secret> — see GET /api/auth/webhook/url."""
    gated = _check_webhook_token()
    if gated is not None:
        return gated
    data = _get_json()
    email = (data.get("email") or "").strip().lower()
    if not email:
        return _err("email required", 422)
    fake = {
        "from_email": email,
        "from_display": f"{data.get('first_name','')} <{email}>".strip(),
        "subject": data.get("subject") or "(no subject)",
        "body": data.get("reply_text") or "",
        "raw_headers": "",
    }
    rec = reply_parser.persist_and_classify(fake)
    if rec["label"] in ("HOT", "MORE_INFO"):
        f = forwarder.forward(rec["id"])
        return _ok({"reply_id": rec["id"], "label": rec["label"], "forwarded": f.get("ok")})
    return _ok({"reply_id": rec["id"], "label": rec["label"], "forwarded": False})


# ── Static pages ────────────────────────────────────────────────────────
@app.route("/")
def index():
    """Operator dashboard. Served from the PROJECT ROOT (one level above
    ai_agency/) not from ai_agency/public/. The public/ subfolder holds
    Caddy-served static assets like /wizard.html and /playbook/*, while
    the operator dashboard lives at the project root alongside
    START-HERE.md. If you preferred to co-locate every static page
    under public/, move this file to ai_agency/public/index.html and
    revert this route to `send_from_directory(PUBLIC_DIR, \"index.html\")`.
    """
    response = send_from_directory(BASE_DIR.parent, "index.html")
    # SPA: never serve stale index.html. Strip ETag + Last-Modified too
    # since send_from_directory emits them by default, and a 304
    # would otherwise let the browser keep yesterday's cached HTML
    # even though the operator's edits have landed on disk.
    response.headers.pop("ETag", None)
    response.headers.pop("Last-Modified", None)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/index.html")
def index_html():
    """Alias for `/` so bookmarks / shared links using the bare
    `index.html` URL still resolve to the operator dashboard instead
    of falling through to Flask's `static_folder = public/` (which
    no longer ships an index.html after the SPA moved to the project
    root). Reuses the `index()` handler so both URLs share the same
    no-cache headers + ETag-strip with no extra redirect hop."""
    return index()


@app.route("/wizard")
@app.route("/wizard/")
def wizard():
    return send_from_directory(PUBLIC_DIR, "wizard.html")


@app.route("/playbook/")
@app.route("/playbook")
def playbook_index():
    return send_from_directory(PLAYBOOK_DIR, "index.html")


@app.route("/playbook/<path:stage>")
def playbook_stage(stage: str):
    candidate = PLAYBOOK_DIR / stage
    if candidate.exists() and candidate.is_file():
        return send_from_directory(PLAYBOOK_DIR, stage)
    abort(404)


@app.route("/healthz")
def healthz():
    return _ok({
        "db": True,
        "scheduler": sched_jobs._scheduler is not None,  # noqa: SLF001
    })


# ── Public, no-auth tracking pixel (proxy-required) ──────────────────────
# Renders a 1x1 transparent GIF so email clients can fetch it. Each fetch
# records an open IF the tracking_id resolves to a known prospect_emails
# row. Always serves the bytes (even on miss) so spam-house scanners don't
# see 4xx and re-fire retry storms that hurt sender reputation.

TRACKING_PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x21\xf9\x04"
    b"\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02"
    b"\x44\x01\x00\x3b"
)
TRACKING_PIXEL_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, private",
    "Pragma": "no-cache",
}


def _is_bot_ua(user_agent: str) -> bool:
    """True when the request UA matches any campaign-configured scanner pattern.

    Reads settings.tracking.bot_user_agents and does a case-insensitive
    substring match. Major corporate mail / URL scanners pre-fetch embedded
    URLs in incoming mail for content inspection and should NOT inflate the
    human open-rate. Empty pattern list disables the check.
    """
    if not user_agent:
        return False
    patterns = (config.load_settings().get("tracking") or {}).get("bot_user_agents") or []
    if not patterns:
        return False
    ua_lower = user_agent.lower()
    lowered = [str(p).lower() for p in patterns]
    return any(p in ua_lower for p in lowered)


@app.route("/t/o/<tracking_id>.gif")
def track_open(tracking_id: str):
    """Tracking-pixel endpoint. Always returns the GIF (even on miss) so
    spam-house scanners do not see 4xx. Always inserts the open (even for
    bot UA hits) so we keep an auditable trail; the is_bot flag lets
    /api/stats/open-rate exclude them from the headline open_rate. IP
    truncation (GDPR) is enforced inside db.record_open.
    """
    row = db.lookup_send_by_tracking(tracking_id)
    if row is not None:
        ua = request.headers.get("User-Agent", "")
        db.record_open(
            row["id"],
            ua,
            request.remote_addr or "",
            is_bot=_is_bot_ua(ua),
        )
    return Response(TRACKING_PIXEL_GIF, mimetype="image/gif", headers=TRACKING_PIXEL_HEADERS)


# ── /api/* blueprints ───────────────────────────────────────────────────
api = Blueprint("api", __name__, url_prefix="/api")


# Settings -----------------------------------------------------------------
@api.route("/settings", methods=["GET"])
def api_settings_get():
    return _ok(config.load_settings())


@api.route("/settings", methods=["PUT"])
def api_settings_put():
    body = _get_json()
    if "key" not in body or "value" not in body:
        return _err("key and value required")
    key = str(body["key"])
    value = body["value"]
    db.set_setting(key, value)
    config.load_settings.cache_clear()  # type: ignore[attr-defined]
    return _ok({key: value})


@api.route("/settings/test-smtp", methods=["POST"])
def api_test_smtp():
    """Open an SMTP connection using the configured credentials and EHLO."""
    import smtplib, ssl
    s = config.load_settings().get("agent", {})
    host = s.get("smtp_host")
    user = s.get("smtp_user")
    pw = s.get("smtp_pass")
    if not (host and user and pw):
        return _err("smtp not configured", 422)
    port = int(s.get("smtp_port") or 587)
    tls = (s.get("smtp_tls") or "starttls").lower()
    try:
        if port == 465 or tls == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as m:
                m.login(user, pw)
        else:
            with smtplib.SMTP(host, port, timeout=15) as m:
                m.ehlo()
                if tls in ("starttls", "tls"):
                    m.starttls(context=ssl.create_default_context())
                    m.ehlo()
                m.login(user, pw)
        return _ok({"smtp": "ok"})
    except Exception as exc:
        return _err(str(exc))


@api.route("/settings/test-imap", methods=["POST"])
def api_test_imap():
    import imaplib
    s = config.load_settings().get("agent", {})
    host = s.get("imap_host")
    user = s.get("imap_user")
    pw = s.get("imap_pass")
    if not (host and user and pw):
        return _err("imap not configured", 422)
    port = int(s.get("imap_port") or 993)
    use_ssl = bool(s.get("imap_ssl", True))
    try:
        c = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        c.login(user, pw)
        c.logout()
        return _ok({"imap": "ok"})
    except Exception as exc:
        return _err(str(exc))


@api.route("/settings/test-tracking", methods=["POST"])
def api_test_tracking():
    """Validate the tracking config. Returns a sample URL the operator
    can hit externally to confirm reachability (e.g. via `curl -I`)."""
    s = (config.load_settings().get("tracking") or {})
    if not s.get("enabled"):
        return _err("tracking not enabled — toggle `tracking.enabled` in /wizard step 5", 422)
    base = (s.get("base_url") or "").strip().rstrip("/")
    if not base:
        return _err(
            "tracking.base_url not set — must be the public https URL behind which "
            "this Flask app is reverse-proxied (e.g. https://agency.example.com)",
            422,
        )
    import secrets
    sample_id = secrets.token_urlsafe(8)
    return _ok({"base_url": base, "sample_url": f"{base}/t/o/{sample_id}.gif"})


@api.route("/stats/open-rate", methods=["GET"])
def api_open_rate():
    """Open-rate aggregate + per-client breakdown.

    Query params: ?since=YYYY-MM-DD (ISO date) to filter to a time window.
    """
    since = request.args.get("since")
    conn = db.get_db()

    args_pe: list[Any] = []
    args_eo: list[Any] = []
    conds_pe: list[str] = []
    conds_eo: list[str] = []
    if since:
        conds_pe.append("sent_at >= ?")
        args_pe.append(since)
        conds_eo.append("eo.opened_at >= ?")
        args_eo.append(since)

    sql_sent = "SELECT COUNT(*) AS c FROM prospect_emails"
    if conds_pe:
        sql_sent += " WHERE " + " AND ".join(conds_pe)
    sent = int(conn.execute(sql_sent, args_pe).fetchone()["c"] or 0)

    # Total opens + real opens (bot-excluded) + bot opens in one round-trip.
    sql_opens_summary = (
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN COALESCE(eo.is_bot, 0) = 0 THEN 1 ELSE 0 END) AS real, "
        "       SUM(CASE WHEN eo.is_bot = 1 THEN 1 ELSE 0 END) AS bot "
        "FROM email_opens eo JOIN prospect_emails pe ON pe.id = eo.prospect_email_id"
    )
    if conds_eo:
        sql_opens_summary += " WHERE " + " AND ".join(conds_eo)
    r = conn.execute(sql_opens_summary, args_eo).fetchone()
    opens = int(r["total"] or 0)
    real_opens = int(r["real"] or 0)
    bot_opens = int(r["bot"] or 0)
    open_rate = round(real_opens / sent, 4) if sent else 0.0

    # Per-client breakdown: real + bot split; open_rate is human-only.
    sql_by_client = (
        "SELECT pe.client_id AS cid, c.name AS cname, "
        "       COUNT(DISTINCT pe.id) AS sent, "
        "       SUM(CASE WHEN eo.id IS NULL OR COALESCE(eo.is_bot, 0) = 0 THEN 0 ELSE 1 END) AS bot_opens, "
        "       SUM(CASE WHEN eo.id IS NOT NULL AND COALESCE(eo.is_bot, 0) = 0 THEN 1 ELSE 0 END) AS real_opens "
        "FROM prospect_emails pe "
        "LEFT JOIN clients c ON pe.client_id=c.id "
        "LEFT JOIN email_opens eo ON pe.id = eo.prospect_email_id"
    )
    args_bc: list[Any] = []
    conds_bc: list[str] = []
    if since:
        conds_bc.append("pe.sent_at >= ?")
        args_bc.append(since)
    if conds_bc:
        sql_by_client += " WHERE " + " AND ".join(conds_bc)
    sql_by_client += " GROUP BY pe.client_id ORDER BY sent DESC LIMIT 50"

    by_client = [
        {
            "client_id": r["cid"],
            "name": r["cname"],
            "sent": int(r["sent"] or 0),
            "opens": int(r["real_opens"] or 0),  # human-only, for back-compat with old UI
            "real_opens": int(r["real_opens"] or 0),
            "bot_opens": int(r["bot_opens"] or 0),
            "open_rate": round(int(r["real_opens"]) / int(r["sent"]), 4) if int(r["sent"]) else 0.0,
        }
        for r in conn.execute(sql_by_client, args_bc).fetchall()
    ]

    return _ok({
        "sent": sent,
        "opens": real_opens,           # back-compat: total human opens
        "real_opens": real_opens,
        "bot_opens": bot_opens,
        "open_rate": open_rate,
        "by_client": by_client,
    })


@api.route("/settings/test-llm", methods=["POST"])
def api_test_llm():
    """
    Verify that the local Ollama LLM is reachable so the wizard can
    fail fast (and not at first campaign send) if Ollama is down or
    the model is not pulled. Calls a tiny idempotent prompt so we get
    a real ChatResponse back, not just an HTTP-up check.

    Wall-clock bounded at 20s because a cold llama3 load can take 30–90s.

    Status codes:
        200 — model replied with text
        422 — user-correctable failure: model name unset, model not pulled,
              empty response
        500 — server-side failure: ollama python pkg missing, ollama daemon
              unreachable, chat timed out
    """
    try:
        import ollama  # type: ignore
    except ImportError:
        # Missing Python dep on the server — operator must install.
        return _err("ollama python package is not installed; run `pip install ollama` on the host", 500)

    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    s = config.load_settings().get("apis", {})
    model = (s.get("ollama_model") or "llama3").strip() or "llama3"

    # 1) Cheap readiness: if ollama is down this will raise.
    try:
        ollama.list()
    except Exception as exc:
        return _err(f"ollama service unreachable on localhost:11434 — {exc}", 500)

    # 2) Tiny prompt — num_predict cap (16 keeps BOS/EOS chatter safe)
    #    + a 20s wall-clock bound via ThreadPoolExecutor since the Ollama
    #    python lib does not expose a per-call timeout.
    def _chat() -> object:
        return ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": "You are a connectivity check. Reply with ONE short word only."},
                {"role": "user", "content": "ping"},
            ],
            options={"num_predict": 16, "temperature": 0},
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_chat)
            try:
                resp = future.result(timeout=20)
            except FuturesTimeoutError:
                return _err(
                    f"ollama chat timed out after 20s — model '{model}' may still be loading its weights. Try again in a minute.",
                    code=500,
                )
    except Exception as exc:
        msg = str(exc)
        if "model" in msg.lower() and "not found" in msg.lower():
            return _err(
                f"ollama is up but model '{model}' is not pulled. "
                f"Run `ollama pull {model}` on the host.",
                code=422,
            )
        return _err(f"ollama chat() failed: {msg}", 500)

    # Ollama python 0.1.x returns a dict-like response; newer versions
    # return a dataclass. Handle both shapes for the message content.
    try:
        text = ((resp.get("message") or {}).get("content") or "").strip()
    except AttributeError:
        text = getattr(resp, "message", None)
        text = getattr(text, "content", "") if text else ""
        text = (text or "").strip()
    if not text:
        return _err(f"ollama replied with empty content from model '{model}'", 422)
    db.audit("llm.test_ok", {"model": model, "reply": text[:40]})
    return _ok({"model": model, "reply": text[:80]})


# Clients ------------------------------------------------------------------
@api.route("/clients", methods=["GET"])
def api_clients_list():
    rows = db.get_db().execute("SELECT * FROM clients ORDER BY created_at DESC").fetchall()
    return _ok([_row_to_dict(r) for r in rows])


@api.route("/clients/<cid>", methods=["GET"])
def api_clients_get(cid: str):
    row = db.get_db().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        return _err("not found", 404)
    return _ok(_row_to_dict(row))


@api.route("/clients", methods=["POST"])
def api_clients_create():
    body = _get_json()
    name = (body.get("name") or "").strip()
    niche = (body.get("niche") or "").strip()
    if not (name and niche):
        return _err("name and niche required")
    cid = db.get_db().execute(
        "INSERT INTO clients(id, name, niche, city, region, country, contact_name, "
        "contact_email, retainer_cents, start_date, notes, status, days_per_week_target, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _new_id(), name, niche,
            body.get("city"), body.get("region"), body.get("country"),
            body.get("contact_name"), body.get("contact_email"),
            int(body.get("retainer_cents") or 0),
            body.get("start_date") or db._now().split("T", 1)[0],
            body.get("notes"), body.get("status") or "active",
            int(body.get("days_per_week_target") or 5),
            db._now(), db._now(),
        ),
    ).lastrowid
    db.audit("client.created", {"client_id": cid, "name": name})
    return _ok({"id": cid})


@api.route("/clients/<cid>", methods=["PUT"])
def api_clients_update(cid: str):
    body = _get_json()
    row = db.get_db().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        return _err("not found", 404)
    fields = {k: body[k] for k in body if k in (
        "name", "niche", "city", "region", "country", "contact_name",
        "contact_email", "retainer_cents", "start_date", "notes",
        "status", "days_per_week_target",
    )}
    if not fields:
        return _ok(_row_to_dict(row))
    sets = ", ".join(f"{k}=?" for k in fields)
    db.get_db().execute(
        f"UPDATE clients SET {sets}, updated_at=? WHERE id=?",
        (*fields.values(), db._now(), cid),
    )
    db.audit("client.updated", {"client_id": cid, "fields": list(fields.keys())})
    return _ok(_row_to_dict(db.get_db().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()))


@api.route("/clients/<cid>", methods=["DELETE"])
def api_clients_delete(cid: str):
    db.get_db().execute(
        "UPDATE clients SET status='paused', updated_at=? WHERE id=?",
        (db._now(), cid),
    )
    db.audit("client.paused", {"client_id": cid})
    return _ok({"id": cid, "status": "paused"})


@api.route("/clients/<cid>/prospects", methods=["GET"])
def api_clients_prospects(cid: str):
    rows = db.get_db().execute(
        "SELECT * FROM prospects WHERE client_id=? ORDER BY score DESC, added_at DESC",
        (cid,),
    ).fetchall()
    return _ok([_row_to_dict(r) for r in rows])


# Prospects ----------------------------------------------------------------
@api.route("/prospects", methods=["GET"])
def api_prospects_list():
    status = request.args.get("status")
    nick = request.args.get("niche")
    sql = "SELECT * FROM prospects"
    conds: list[str] = []
    args: list[Any] = []
    if status:
        conds.append("status=?")
        args.append(status)
    if nick:
        conds.append("niche=?")
        args.append(nick)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY score DESC, added_at DESC LIMIT 200"
    rows = db.get_db().execute(sql, args).fetchall()
    return _ok([_row_to_dict(r) for r in rows])


@api.route("/prospects", methods=["POST"])
def api_prospects_create():
    body = _get_json()
    name = (body.get("business_name") or body.get("name") or "").strip()
    niche = (body.get("niche") or "").strip()
    if not (name and niche):
        return _err("business_name and niche required")
    pid = db.get_db().execute(
        "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
        "website, phone, contact_name, contact_email, contact_title, source, external_id, "
        "score, status, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _new_id(), body.get("client_id"), name, niche,
            body.get("city"), body.get("region"), body.get("country"),
            body.get("website"), body.get("phone"),
            body.get("contact_name"), body.get("contact_email"),
            body.get("contact_title"),
            body.get("source") or "Manual",
            f"manual:{_new_id()[:12]}",
            int(body.get("score") or 0),
            body.get("status") or "new",
            db._now(),
        ),
    ).lastrowid
    db.audit("prospect.created", {"prospect_id": pid, "name": name})
    return _ok({"id": pid})


@api.route("/prospects/<pid>", methods=["DELETE"])
def api_prospects_delete(pid: str):
    db.get_db().execute("DELETE FROM prospects WHERE id=?", (pid,))
    return _ok({"id": pid, "deleted": True})


# Warmups ------------------------------------------------------------------
@api.route("/warmups", methods=["GET"])
def api_warmups_list():
    rows = db.get_db().execute("SELECT * FROM warmups ORDER BY start_date DESC").fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["day_index"] = sched_jobs._day_index(r["start_date"]) if r.get("start_date") else 0
        out.append(d)
    return _ok(out)


@api.route("/warmups", methods=["POST"])
def api_warmups_create():
    body = _get_json()
    domain = (body.get("domain") or "").strip().lower()
    from_addr = (body.get("from_address") or f"hello@{domain}").strip().lower()
    if not domain:
        return _err("domain required")
    try:
        wid = db.get_db().execute(
            "INSERT INTO warmups(id, domain, from_address, client_id, start_date, "
            "target_daily, tool, state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                _new_id(), domain, from_addr, body.get("client_id"),
                body.get("start_date") or db._now().split("T", 1)[0],
                int(body.get("target_daily") or 20),
                body.get("tool") or "self",
                "warming", db._now(), db._now(),
            ),
        ).lastrowid
        db.audit("warmup.created", {"warmup_id": wid, "domain": domain})
        return _ok({"id": wid})
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return _err("domain already in warmup", 409)
        return _err(str(exc))


# Niches -------------------------------------------------------------------
@api.route("/niches", methods=["GET"])
def api_niches():
    return _ok(config.load_niches())


# Replies ------------------------------------------------------------------
@api.route("/replies", methods=["GET"])
def api_replies():
    intent = request.args.get("intent")
    sql = "SELECT r.*, p.business_name AS prospect_name, c.name AS client_name FROM replies r " \
          "LEFT JOIN prospects p ON r.prospect_id=p.id LEFT JOIN clients c ON r.client_id=c.id"
    args: list[Any] = []
    if intent:
        sql += " WHERE r.intent=?"
        args.append(intent)
    sql += " ORDER BY r.received_at DESC LIMIT 200"
    rows = db.get_db().execute(sql, args).fetchall()
    return _ok([_row_to_dict(r) for r in rows])


# Outbound email -----------------------------------------------------------
@api.route("/email/send", methods=["POST"])
def api_email_send():
    body = _get_json()
    pid = body.get("prospect_id")
    step = body.get("step") or "cold"
    prospect = db.get_db().execute(
        "SELECT * FROM prospects WHERE id=?", (pid,)
    ).fetchone() if pid else None
    if prospect is None:
        return _err("prospect_id required and must exist", 422)

    p = dict(prospect)
    if step == "cold":
        gen = generator.generate_cold_email(
            niche=p["niche"], business=p["business_name"],
            contact=p.get("contact_name"), city=p.get("city"),
        )
    else:
        step_num = 1 if step == "followup1" else 2
        gen = generator.generate_followup(
            niche=p["niche"], business=p["business_name"],
            contact=p.get("contact_name"), city=p.get("city"),
            step=step_num,
        )
    if not p.get("contact_email"):
        return _ok({"draft": gen, "sent": False, "reason": "no_contact_email"}, preview=True)
    res = sender.send_to_prospect(
        prospect_id=p["id"], client_id=p.get("client_id"),
        to_email=p["contact_email"], to_name=p.get("contact_name"),
        subject=gen["subject"], body=gen["body"], step=step,
    )
    return _ok({"draft": gen, "send_result": res})


@api.route("/email/remaining-quota", methods=["GET"])
def api_email_quota():
    s = config.load_settings().get("agent", {})
    addr = s.get("from_address") or s.get("smtp_user") or ""
    return _ok({
        "from_address": addr,
        "used_today": db.quota_today(addr),
        "remaining": sender.quota_remaining(addr),
    })


# Hunts / Briefs / Inbox check --------------------------------------------
@api.route("/hunt", methods=["POST"])
def api_hunt():
    body = _get_json()
    cid = body.get("client_id")
    if cid:
        row = db.get_db().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        if not row:
            return _err("client not found", 404)
        a = apollo.run_for_client(dict(row)) if apollo.enabled() else {"found": 0, "inserted": 0}
        g = google_places.run_for_client(dict(row)) if google_places.enabled() else {"found": 0, "inserted": 0}
        return _ok({"apollo": a, "places": g})
    return _ok(sched_jobs.nightly_hunt())


@api.route("/brief", methods=["POST"])
def api_brief():
    return _ok(sched_jobs.morning_brief())


@api.route("/check-inbox", methods=["POST"])
def api_check_inbox():
    return _ok(sched_jobs.reply_poller())


@api.route("/jobs/run", methods=["POST"])
def api_jobs_run():
    body = _get_json()
    name = body.get("name")
    if name == "nightly_hunt":
        return _ok(sched_jobs.nightly_hunt())
    if name == "morning_brief":
        return _ok(sched_jobs.morning_brief())
    if name == "reply_poller":
        return _ok(sched_jobs.reply_poller())
    if name == "warmup_tick":
        return _ok(sched_jobs.warmup_tick())
    return _err(f"unknown job: {name}", 422)


@api.route("/dashboard/summary", methods=["GET"])
def api_dashboard_summary():
    conn = db.get_db()
    clients = conn.execute("SELECT COUNT(*) AS c FROM clients WHERE status='active'").fetchone()["c"]
    prospects = conn.execute("SELECT COUNT(*) AS c FROM prospects").fetchone()["c"]
    contacted = conn.execute("SELECT COUNT(*) AS c FROM prospects WHERE status IN ('contacted','replied','booked','closed')").fetchone()["c"]
    hot = conn.execute("SELECT COUNT(*) AS c FROM replies WHERE intent IN ('HOT','MORE_INFO')").fetchone()["c"]
    forwards = conn.execute("SELECT COUNT(*) AS c FROM replies WHERE forwarded=1").fetchone()["c"]
    warmups_active = conn.execute("SELECT COUNT(*) AS c FROM warmups WHERE state='warming'").fetchone()["c"]
    s = config.load_settings().get("agent", {})
    quota_addr = s.get("from_address") or s.get("smtp_user") or ""
    quota_used = db.quota_today(quota_addr)
    quota_cap = int((config.load_settings().get("warmup", {}) or {}).get("max_daily_after") or 40)
    return _ok({
        "clients": clients,
        "prospects": prospects,
        "contacted": contacted,
        "hot_replies": hot,
        "forwarded_leads": forwards,
        "warmups_active": warmups_active,
        "quota_used_today": quota_used,
        "quota_cap": quota_cap,
        "scheduler_running": sched_jobs._scheduler is not None,  # noqa: SLF001
    })


# the BLUEPRINT_AUTH_EXEMPT allow-list below).

def _enforce_json_on_mutations() -> tuple[Any, int] | None:
    """Reject cross-site form-encoded POSTs. Combined with SameSite=Lax, this
    blocks CSRF for our JSON-only mutating endpoints."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        ct = (request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ct and ct != "application/json":
            return _err("content_type_must_be_application_json", 415)
    return None


@api.before_request
def api_before_request():
    # 1) Authentication: must be authed, except for a small allow-list.
    endpoint = request.endpoint or ""
    if not auth.is_authed() and endpoint not in (
        "auth.status",
        "auth.install_token",
        "auth.install",
        "auth.login",
        "track_open",  # tracking pixel must stay public
    ):
        db.audit("api.unauthorized", {"endpoint": endpoint, "method": request.method})
        return _err("unauthorized", 401)
    # 2) CSRF mitigation: mutating endpoints require application/json.
    bad = _enforce_json_on_mutations()
    if bad is not None:
        return bad
app.register_blueprint(api)
app.register_blueprint(auth.auth_bp)


# ── local helpers ────────────────────────────────────────────────────────
def _new_id() -> str:
    import secrets
    return secrets.token_hex(8)


# ── Entrypoint ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
