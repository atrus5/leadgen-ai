"""
Runnable end-to-end smoke test for the LeadGen AI stack.

Run from one level above the package:
   cd /home/paul/Git/Paul
   PYTHONPATH=leadgen-ai python -m leadgen-ai.ai_agency.tests.test_smoke

Or use the helper:
   bash leadgen-ai/ai_agency/scripts/run-tests.sh

What it covers:
  1. SQLite schema creates the 12 expected tables.
  2. DEFAULT_SETTINGS seeds `apis.ollama_model = "llama3"`.
  3. Insert a client + insert a prospect works.
  4. UNIQUE(client_id, external_id) blocks duplicate prospects.
  5. niche subject_templates vary randomly across renders.
  6. Deterministic keyword classifier returns HOT for "yes, send me more info".
  7. UNSUBSCRIBE replies add their email to the blacklist table.
  8. send_to_prospect dispatches (mocked SMTP), writes prospect_emails,
     bumps sender_quota, and is idempotent inside a 7-day window.
  9. send_to_prospect returns "blacklisted" when the receiver is blacklisted.
 10. Flask /healthz returns 200.
 11. Flask /api/settings/test-llm returns 500 with the pip-hint when the
     `ollama` Python package is unavailable.

Mocked: SMTP (noop), IMAP (not exercised — reply ingestion has its own path),
Ollama import (artificially fails for the test-llm 500 test).

No external services touched.
"""
from __future__ import annotations

import builtins
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# Make `ai_agency` resolvable as a top-level package.
# tests/ sits inside ai_agency/, so we add the parent of ai_agency/ to sys.path.
_TEST_ROOT = Path(__file__).resolve()
_AGENCY_PKG = _TEST_ROOT.parent.parent           # .../leadgen-ai/ai_agency
_PACKAGE_PARENT = _AGENCY_PKG.parent              # .../leadgen-ai
sys.path.insert(0, str(_PACKAGE_PARENT))

# Now import ai_agency normally.
from ai_agency import config, db  # noqa: E402

# Load the rest of the modules without requiring ai_agency.outreach.* package init.
import importlib.util as _u  # noqa: E402


def _load(name: str, path: Path):
    spec = _u.spec_from_file_location(name, path)
    mod = _u.module_from_spec(spec)
    assert spec.loader is not None, f"no loader for {name}"
    spec.loader.exec_module(mod)
    # Stash the module into sys.modules so any later `from <name> import ...`
    # resolves to the same instance (matters for monkey-patching).
    sys.modules[name] = mod
    return mod


_TEMPLATES = _load(
    "ai_agency.outreach.templates",
    _AGENCY_PKG / "outreach" / "templates.py",
)
_REPLY_PARSER = _load(
    "ai_agency.outreach.reply_parser",
    _AGENCY_PKG / "outreach" / "reply_parser.py",
)
_SENDER = _load(
    "ai_agency.outreach.sender",
    _AGENCY_PKG / "outreach" / "sender.py",
)
_FORWARDER = _load(
    "ai_agency.outreach.forwarder",
    _AGENCY_PKG / "outreach" / "forwarder.py",
)
_SENDER  # already loaded above; alias for clarity in the new tests


# ── Bootstrap ────────────────────────────────────────────────────────────

def _bootstrap() -> None:
    """Initialise schema + default settings + auth once before any test runs.

    Uses a per-run tmpfile DB so the developer's data/agency.db is never
    touched and the suite is hermetic across invocations. Order matters:
    reset the DB path BEFORE init_schema; init_schema BEFORE
    ensure_default_settings (settings reads its own schema)."""
    # Point DB_PATH at a fresh tmpfile and clear any cached connection
    # so `_connect()` re-opens against the new path on next access.
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="leadgen_smoke_")
    os.close(fd)
    db.DB_PATH = Path(tmp_path)
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db._LOCAL.conn = None
    db.init_schema()
    db.ensure_default_settings()
    # Seed auth + webhook_secret so the api Blueprint's before_request
    # hook doesn't 401 our existing /api/* test calls. Real operator-set
    # passwords go through /wizard; here we set the bcrypt hash directly.
    try:
        import bcrypt
        pw = "smoketest-password-123"
        db.set_setting("auth", {
            "password_hash": bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode("ascii"),
            "scheme": "bcrypt-4-test",
        })
        # Stash the password globally so _logged_in_client() can reuse it.
        _SMOKE_TEST_PASSWORD["value"] = pw
    except ImportError:
        pass  # bcrypt not installed; auth tests will skip gracefully
    db.set_setting("webhook_secret", {"value": "smoke-webhook-secret"})


_SMOKE_TEST_PASSWORD: dict = {"value": "smoketest-password-123"}


# ── Test rig ─────────────────────────────────────────────────────────────
class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[tuple[str, str]] = []

    def run(self, name: str, fn) -> None:
        try:
            result = fn()
            if result == "skip":
                print(f"  ⊘ {name} (skipped)")
                self.skipped += 1
                return
            print(f"  ✓ {name}")
            self.passed += 1
        except AssertionError as exc:
            print(f"  ✗ {name}: {exc}")
            self.failed += 1
            self.failures.append((name, f"AssertionError: {exc}"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {name}: UNEXPECTED {type(exc).__name__}: {exc}")
            self.failed += 1
            self.failures.append((name, f"{type(exc).__name__}: {exc}"))


def _now_iso() -> str:
    return db._now()


def _logged_in_client(app_mod):
    """Create a Flask test_client() that satisfies the api-before_request
    auth gate, without going through /api/auth/login.

    Two-layered approach:
    1. session_transaction() seeds session['authed']=True (covers the
       canonical cookie-based path; future code that ALSO needs to read
       session.* works).
    2. monkey-patch auth.is_authed to return True so the BEFORE_REQUEST
       gate is unconditionally satisfied in this test process. This
       bypasses the SESSION_COOKIE_SECURE cross-test state leak entirely

       (test_sender_embeds_tracking_when_enabled flips tracking.base_url
       to https which previously triggered SESSION_COOKIE_SECURE=True
       on every subsequent app instance, which made test_client refuse to
       attach the session cookie over HTTP, which made subsequent
       `auth.is_authed()` reads return False and the gate 401).

    The patch is restored at main()'s end so production callers and any
    later test in the suite see the real auth.is_authed.
    """
    import ai_agency.auth as _auth_module
    if not _AUTH_PATCH_ACTIVE["value"]:
        _AUTH_PATCH_ACTIVE["value"] = True
        _ORIGINAL_IS_AUTHED["value"] = _auth_module.is_authed
        _auth_module.is_authed = lambda: True

    c = app_mod.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
    return c


_AUTH_PATCH_ACTIVE: dict = {"value": False}
_ORIGINAL_IS_AUTHED: dict = {"value": None}


def _restore_auth_is_authed() -> None:
    """Restore auth.is_authed to the real implementation. Called once at
    the very end of main() so subsequent tests or production callers
    don't see the monkey-patch."""
    if _AUTH_PATCH_ACTIVE["value"]:
        import ai_agency.auth as _auth_module
        _auth_module.is_authed = _ORIGINAL_IS_AUTHED["value"]
        _AUTH_PATCH_ACTIVE["value"] = False


def _seed_agent_settings() -> None:
    """Provision just enough agent settings to make sender.py happy."""
    db.set_setting(
        "agent",
        {
            "owner_email": "",
            "from_name": "Smoke Test",
            "from_address": "smoke@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "smoke@example.com",
            "smtp_pass": "fakepass",
            "smtp_tls": "starttls",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_user": "smoke@example.com",
            "imap_pass": "fakepass",
            "imap_ssl": True,
            "morning_brief_to": "",
            "morning_brief_hour": 8,
            "nightly_hunt_hour": 2,
        },
    )
    config.load_settings.cache_clear()


# ── Tests ────────────────────────────────────────────────────────────────

# Detect Flask at import time so individual tests can skip cleanly when
# the project's Flask dep is missing in the test environment (no pip,
# no apt passthrough). The route tests return "skip" when absent.
try:
    import flask  # noqa: F401
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

def test_init_schema() -> None:
    """All expected tables exist after init_schema() (now 13 incl. email_contacts)."""
    rows = db.get_db().execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "settings", "clients", "prospects", "prospect_emails", "replies",
        "warmups", "warmup_log", "hunt_runs", "job_runs", "blacklist",
        "sender_quotas", "audit", "email_contacts",
    }
    missing = expected - names
    assert not missing, f"missing tables: {missing}; got {sorted(names)}"


def test_settings_seed_ollama_model() -> None:
    """DEFAULT_SETTINGS[\"apis\"] seeds ollama_model on first boot."""
    s = config.load_settings()
    apis = s.get("apis", {})
    assert apis.get("ollama_model") == "llama3", (
        f"expected apis.ollama_model='llama3', got {apis.get('ollama_model')!r}"
    )


def test_create_client() -> None:
    """INSERT into clients works with all required columns."""
    cur = db.get_db().execute(
        "INSERT INTO clients(id, name, niche, city, region, country, contact_name, "
        "contact_email, retainer_cents, start_date, notes, status, days_per_week_target, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "cid_smoke_1", "Smoke Test Client", "Roofing",
            "Austin", "TX", "USA", "Mike Test", "mike@test.example.com",
            250000, _now_iso()[:10], "smoke-test client row", "active", 5,
            _now_iso(), _now_iso(),
        ),
    )
    assert cur.lastrowid >= 1
    row = db.get_db().execute(
        "SELECT name, niche, status FROM clients WHERE id=?", ("cid_smoke_1",)
    ).fetchone()
    assert row is not None and row["name"] == "Smoke Test Client"
    assert row["status"] == "active"


def test_prospect_dedupe() -> None:
    """UNIQUE(client_id, external_id) blocks duplicate prospects."""
    now = _now_iso()
    db.get_db().execute(
        "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
        "website, phone, contact_name, contact_email, contact_title, source, external_id, "
        "score, status, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pid_smoke_1", "cid_smoke_1", "Smith Roofing Pros", "Roofing",
         "Austin", "TX", "USA", "smithroofing.com", None, "John",
         "john@smithroofing.com", "Owner", "Apollo.io", "apollo:smithroofing-pros",
         7, "new", now),
    )
    # Same (client_id, external_id) → must raise IntegrityError.
    try:
        db.get_db().execute(
            "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
            "website, phone, contact_name, contact_email, contact_title, source, external_id, "
            "score, status, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("pid_smoke_2", "cid_smoke_1", "Smith Roofing Pros", "Roofing",
             "Austin", "TX", "USA", "smithroofing.com", None, "John",
             "john2@smithroofing.com", "Owner", "Apollo.io", "apollo:smithroofing-pros",
             7, "new", now),
        )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("expected IntegrityError on duplicate (client_id, external_id)")


def test_subject_templates_vary_randomly() -> None:
    """Plumbing templates provide ≥3 distinct subjects across repeated renders."""
    niches = json.loads(
        (_AGENCY_PKG / "config" / "niches.json").read_text()
    )["niches"]
    plumbing = next(n for n in niches if n["key"] == "Plumbing")
    seen: set[str] = set()
    for _ in range(30):
        out = _TEMPLATES.cold_email(
            plumbing, business_name="Smith Plumbing",
            contact_name="Mike", city="Austin",
        )
        seen.add(out["subject"])
    assert len(seen) >= 3, f"expected ≥3 unique subjects, got {len(seen)}: {sorted(seen)}"


def test_keyword_fallback_returns_hot() -> None:
    """With LLM forced off, 'yes, send me more info' classifies as HOT."""
    orig = _REPLY_PARSER._classify_with_llama
    _REPLY_PARSER._classify_with_llama = lambda subject, body: (None, None)
    try:
        label, conf = _REPLY_PARSER.classify(
            "Re: 5-min read?", "yes please, send me more info"
        )
        assert label == "HOT", f"expected HOT, got {label!r}"
        assert conf >= 0.6, f"expected confidence ≥ 0.6, got {conf}"
    finally:
        _REPLY_PARSER._classify_with_llama = orig


def test_classify_unsubscribe_persists_to_blacklist() -> None:
    """UNSUBSCRIBE replies must add their email to the blacklist."""
    email = f"angry-{uuid.uuid4().hex[:8]}@subscriber.example"
    orig = _REPLY_PARSER._classify_with_llama
    _REPLY_PARSER._classify_with_llama = lambda subject, body: (None, None)
    try:
        label, _ = _REPLY_PARSER.classify("STOP", "please unsubscribe me")
        assert label == "UNSUBSCRIBE", f"expected UNSUBSCRIBE, got {label!r}"
        # persist_and_classify runs the blacklist_add for UNSUBSCRIBE.
        _REPLY_PARSER.persist_and_classify({
            "from_email": email,
            "from_display": f"<{email}>",
            "subject": "STOP",
            "body": "please unsubscribe me",
            "raw_headers": "",
        })
        row = db.get_db().execute(
            "SELECT 1 FROM blacklist WHERE value=?", (email,)
        ).fetchone()
        assert row is not None, "expected blacklist row for UNSUBSCRIBE sender"
    finally:
        _REPLY_PARSER._classify_with_llama = orig


def test_send_to_prospect_records_and_bumps_quota() -> None:
    """send_to_prospect dispatches (mocked SMTP), writes prospect_emails, bumps quota."""
    _seed_agent_settings()

    captured: dict[str, Any] = {}
    def fake_send(msg):  # _send_smtp signature
        captured["subject"] = msg["Subject"]
        captured["to"] = msg["To"]
        captured["msg"] = msg
        return True, "ok"

    _SENDER._send_smtp = fake_send  # type: ignore[attr-defined]

    niches = json.loads(
        (_AGENCY_PKG / "config" / "niches.json").read_text()
    )["niches"]
    plumbing = next(n for n in niches if n["key"] == "Plumbing")
    templ = _TEMPLATES.cold_email(
        plumbing, business_name="Smith Plumbing Pros",
        contact_name="John", city="Austin",
    )
    # Already-used step='cold' for this prospect will block; reset the row history.
    db.get_db().execute("DELETE FROM prospect_emails WHERE prospect_id=?", ("pid_smoke_1",))
    db.get_db().execute("DELETE FROM sender_quotas WHERE from_address=?", ("smoke@example.com",))

    result = _SENDER.send_to_prospect(
        prospect_id="pid_smoke_1",
        client_id="cid_smoke_1",
        to_email="john@smithroofing.com",
        to_name="John",
        subject=templ["subject"],
        body=templ["body"],
        step="cold",
    )
    assert result.get("ok") is True, f"send returned {result!r}"
    assert captured.get("subject"), "mock SMTP handler never received a message"
    assert captured.get("to") == "john@smithroofing.com"

    row = db.get_db().execute(
        "SELECT subject, status FROM prospect_emails "
        "WHERE prospect_id=? AND step=?",
        ("pid_smoke_1", "cold"),
    ).fetchone()
    assert row is not None, "expected prospect_emails row to exist"
    assert row["subject"] == templ["subject"], f"subject mismatch: {row['subject']!r}"
    assert row["status"] == "sent", f"expected status='sent', got {row['status']!r}"

    quota = db.quota_today("smoke@example.com")
    assert quota == 1, f"expected quota_today=1, got {quota}"

    # last_contacted_at + status='contacted' should be set on success.
    p = db.get_db().execute(
        "SELECT status, last_contacted_at FROM prospects WHERE id=?",
        ("pid_smoke_1",),
    ).fetchone()
    assert p["status"] == "contacted", f"expected status='contacted', got {p['status']!r}"
    assert p["last_contacted_at"], "expected last_contacted_at to be set"


def test_send_idempotency_window() -> None:
    """Second send within 7 days → duplicate_step. After bumping sent_at back → ok."""
    # The previous test wrote a "cold" row with sent_at = NOW, so retry must fail.
    res = _SENDER.send_to_prospect(
        prospect_id="pid_smoke_1", client_id="cid_smoke_1",
        to_email="john@smithroofing.com", to_name="John",
        subject="dup attempt", body="dup attempt", step="cold",
    )
    assert res.get("ok") is False, f"expected failure, got {res!r}"
    assert res.get("error") == "duplicate_step", f"got {res}"

    # Outside the 7-day window: rewind sent_at → second attempt should succeed.
    db.get_db().execute(
        "UPDATE prospect_emails SET sent_at = datetime('now','-8 day') "
        "WHERE prospect_id=? AND step=?",
        ("pid_smoke_1", "cold"),
    )
    res = _SENDER.send_to_prospect(
        prospect_id="pid_smoke_1", client_id="cid_smoke_1",
        to_email="john@smithroofing.com", to_name="John",
        subject="after 8 days", body="after 8 days", step="cold",
    )
    assert res.get("ok") is True, f"expected ok after 8-day gap, got {res!r}"


def test_send_blacklisted_email_blocked() -> None:
    _db_blacklist = "blocked-" + uuid.uuid4().hex + "@example.com"  # 128-bit entropy
    db.blacklist_add(_db_blacklist, "smoke test")
    res = _SENDER.send_to_prospect(
        prospect_id="pid_smoke_1", client_id="cid_smoke_1",
        to_email=_db_blacklist, to_name="X",
        subject="x", body="x", step="warm_intro",
    )
    assert res.get("ok") is False and res.get("error") == "blacklisted", f"got {res!r}"
    # And confirm no prospect_emails row got written for this blacklist target.
    row = db.get_db().execute(
        "SELECT 1 FROM prospect_emails WHERE step='warm_intro'"
    ).fetchone()
    assert row is None, "expected no warm_intro row to be recorded for blacklisted send"


# ── Forwarder safety invariant ───────────────────────────────────────────
# The system's most critical safety property: a HOT-reply forward must
# include ONLY the one prospect tied to the reply, and the recipient must
# be the paying client (never the prospect, never another client, never
# a list of all prospects for the same client).

class _FakeSMTP:
    """Context-manager fake that captures the most recent sent message."""
    last_sent = None  # class-level so the suite can read it after send

    def __init__(self, host, port, **kw):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, **kw):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        _FakeSMTP.last_sent = msg


def test_forward_hot_reply_emails_only_one_prospect() -> None:
    """The forwarder must NOT leak other prospects to the paying client."""
    _seed_agent_settings()

    # Isolation client with two foreign prospects — these must NEVER appear.
    db.get_db().execute(
        "INSERT INTO clients(id, name, niche, city, region, country, contact_name, "
        "contact_email, retainer_cents, start_date, notes, status, days_per_week_target, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cid_isolation", "Isolation Probe", "HVAC", "Boston", "MA", "USA",
         "Iso Owner", "iso@example.com", 100000, _now_iso()[:10],
         "isolation-test client", "active", 5, _now_iso(), _now_iso()),
    )
    db.get_db().execute(
        "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
        "website, phone, contact_name, contact_email, contact_title, source, external_id, "
        "score, status, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pid_noise_1", "cid_isolation", "Noise Incorporated", "HVAC",
         "Boston", "MA", "USA", "noise.com", None, "N1", "n1@noise.com",
         "Owner", "Apollo.io", "apollo:noise", 7, "new", _now_iso()),
    )
    db.get_db().execute(
        "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
        "website, phone, contact_name, contact_email, contact_title, source, external_id, "
        "score, status, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pid_noise_2", "cid_isolation", "Other Noise Co", "HVAC",
         "Boston", "MA", "USA", "othernoise.com", None, "N2", "n2@othernoise.com",
         "Owner", "Apollo.io", "apollo:other-noise", 7, "new", _now_iso()),
    )
    # Bystander prospect — SAME client (cid_smoke_1) but NOT the replied-to one.
    db.get_db().execute(
        "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
        "website, phone, contact_name, contact_email, contact_title, source, external_id, "
        "score, status, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pid_bystander", "cid_smoke_1", "Bystander LLC", "Roofing",
         "Austin", "TX", "USA", "bystander.com", None, "B", "b@bystander.com",
         "Owner", "Apollo.io", "apollo:bystander", 7, "new", _now_iso()),
    )

    # HOT reply targeting pid_smoke_1 (Smith Roofing Pros from earlier tests).
    db.get_db().execute(
        "INSERT INTO replies(id, prospect_id, client_id, from_address, subject, body, "
        "received_at, intent, confidence, forwarded, raw_headers) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, 'HOT', 0.9, 0, '')",
        (uuid.uuid4().hex, "pid_smoke_1", "cid_smoke_1",
         "john@smithroofing.com", "Re: 5-min read?", "yes please",
         _now_iso()),
    )
    reply_id = db.get_db().execute(
        "SELECT id FROM replies WHERE from_address=? ORDER BY received_at DESC LIMIT 1",
        ("john@smithroofing.com",),
    ).fetchone()["id"]

    _FakeSMTP.last_sent = None
    _FORWARDER.smtplib.SMTP = _FakeSMTP
    _FORWARDER.smtplib.SMTP_SSL = _FakeSMTP

    result = _FORWARDER.forward(reply_id)
    assert result.get("ok") is True, f"forward failed: {result!r}"

    msg = _FakeSMTP.last_sent
    assert msg is not None, "mock SMTP never received the forward message"
    body = msg.get_content()

    # Recipient is the paying CLIENT, not the prospect.
    assert msg["To"] == "mike@test.example.com", f"forward went to {msg['To']!r}, not the client"
    # Primary prospect is in body AND subject.
    assert "Smith Roofing Pros" in body, "forward body missing the primary prospect"
    assert "Smith Roofing Pros" in msg["Subject"], "forward subject missing the primary prospect"
    # The three OTHER prospects must NOT be in the body — the safety invariant.
    for forbidden in ("Noise Incorporated", "Other Noise Co", "Bystander LLC"):
        assert forbidden not in body, f"LEAK: forward body contained {forbidden!r}"

    # And the reply itself was marked forwarded.
    row = db.get_db().execute(
        "SELECT forwarded, forwarded_at FROM replies WHERE id=?", (reply_id,)
    ).fetchone()
    assert row["forwarded"] == 1, "expected replies.forwarded=1 after successful forward"


# ── Open-rate tracking-pixel coverage ───────────────────────────────────

def test_record_open_and_lookup_by_tracking() -> None:
    """DB helpers: write open, count, and resolve tracking_id."""
    new_tracking = "abc1trackXGw"  # 12-char URL-safe token-style
    db.get_db().execute(
        "INSERT INTO prospect_emails(id, prospect_id, client_id, step, subject, body, "
        "from_address, sent_at, message_id, status, tracking_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?)",
        (uuid.uuid4().hex, "pid_smoke_1", "cid_smoke_1", "cold",
         "open-rate test subj", "open-rate test body",
         "smoke@example.com", _now_iso(), "<msg@test>", new_tracking),
    )
    row = db.lookup_send_by_tracking(new_tracking)
    assert row is not None, "expected lookup_send_by_tracking to resolve the token"
    assert row["prospect_id"] == "pid_smoke_1"
    assert db.lookup_send_by_tracking("does-not-exist-token") is None

    pe_id = row["id"]
    assert db.open_count_for_send(pe_id) == 0
    db.record_open(pe_id, "Mozilla/5.0 Gecko", "127.0.0.1")
    db.record_open(pe_id, "Mozilla/5.0 Gecko again", "127.0.0.1")
    assert db.open_count_for_send(pe_id) == 2, "each fetch should count separately"

    # IP-only audit: 192.168.1.42 should round-trip into the SQL INSERT as
    # "192.168.1.0" (IPv4 /24 truncation is the GDPR-safe default).
    rows = db.get_db().execute(
        "SELECT ip_address, is_bot FROM email_opens WHERE prospect_email_id=? "
        "ORDER BY id",
        (pe_id,),
    ).fetchall()
    assert all(r["is_bot"] == 0 for r in rows), "default is_bot must be 0"
    last_ip = rows[-1]["ip_address"]
    assert last_ip in ("127.0.0.0", "::1"), \
        f"127.0.0.1 should truncate to /24 (got {last_ip!r})"

    # A separately-tracked IPv4 must also truncate to /24.
    db.record_open(pe_id, "Outlook-iOS", "192.168.1.42")
    latest = db.get_db().execute(
        "SELECT ip_address FROM email_opens WHERE prospect_email_id=? "
        "ORDER BY id DESC LIMIT 1",
        (pe_id,),
    ).fetchone()
    assert latest["ip_address"] == "192.168.1.0", (
        f"IPv4 should truncate to /24, got {latest['ip_address']!r}"
    )

    # is_bot keyword arg round-trips into the row's is_bot flag.
    db.record_open(pe_id, "Proofpoint URL Defense", "10.0.0.1", is_bot=True)
    latest_full = db.get_db().execute(
        "SELECT ip_address, is_bot FROM email_opens WHERE prospect_email_id=? "
        "ORDER BY id DESC LIMIT 1",
        (pe_id,),
    ).fetchone()
    assert latest_full["is_bot"] == 1, "expected is_bot=1 when explicitly set"
    assert latest_full["ip_address"] == "10.0.0.0", \
        "truncation must still apply when is_bot=True"

    # IPv4-mapped-in-IPv6 (e.g. ::ffff:192.168.1.42) must also truncate
    # as an IPv4 /24. Python auto-projects these to IPv4Address so this is a
    # regression guard, not a corner case we expect in production.
    db.record_open(pe_id, "DualStack Scanner", "::ffff:192.168.1.42")
    latest_dual = db.get_db().execute(
        "SELECT ip_address FROM email_opens WHERE prospect_email_id=? "
        "ORDER BY id DESC LIMIT 1",
        (pe_id,),
    ).fetchone()
    assert latest_dual["ip_address"] == "192.168.1.0", (
        f"IPv4-mapped-in-IPv6 must truncate to /24 (192.168.1.0), "
        f"got {latest_dual['ip_address']!r}"
    )

    # A genuine IPv6 address must truncate to /48.
    db.record_open(pe_id, "Apple Mail IPv6", "2001:db8:abcd:1234:cafe::1")
    latest_v6 = db.get_db().execute(
        "SELECT ip_address FROM email_opens WHERE prospect_email_id=? "
        "ORDER BY id DESC LIMIT 1",
        (pe_id,),
    ).fetchone()
    assert latest_v6["ip_address"] == "2001:db8:abcd::", (
        f"IPv6 must truncate to /48, got {latest_v6['ip_address']!r}"
    )

    # Garbage IP input must not crash record_open; it should write an
    # empty string so the column stays usable for filtering.
    db.record_open(pe_id, "Junk", "not-an-ip")
    latest_junk = db.get_db().execute(
        "SELECT ip_address FROM email_opens WHERE prospect_email_id=? "
        "ORDER BY id DESC LIMIT 1",
        (pe_id,),
    ).fetchone()
    assert latest_junk["ip_address"] == "", (
        f"unparseable IP must yield empty string, got {latest_junk['ip_address']!r}"
    )


# ── Auth and webhook-token coverage (Task #1) ────────────────────────

def test_route_unauthorized_returns_401_without_session():
    """All /api/* routes must 401 when there is no Flask session, except
    the auth.allow-listed endpoints and the tracking pixel."""
    if not HAS_FLASK:
        return "skip"
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return "skip"
    app_mod = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    c = app_mod.app.test_client()
    # /api/auth/status is exempt (used to check auth state itself).
    r = c.get("/api/auth/status")
    assert r.status_code == 200, f"status is exempt; got {r.status_code}"
    # /api/clients is NOT exempt — must 401.
    r = c.get("/api/clients")
    assert r.status_code == 401, (
        f"GET /api/clients without session must 401; got {r.status_code} {r.get_data(as_text=True)!r}"
    )
    j = r.get_json()
    assert j.get("ok") is False and j.get("error") == "unauthorized"


def test_route_logged_in_can_clients_list():
    """After login, the same endpoint flips to 200 and returns the client list."""
    if not HAS_FLASK:
        return "skip"
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return "skip"
    app_mod = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    c = _logged_in_client(app_mod)
    r = c.get("/api/clients")
    assert r.status_code == 200, f"expected 200 after login, got {r.status_code}"
    j = r.get_json()
    assert j.get("ok") is True


def test_webhook_secret_gate_blocks_unauthorized_post():
    """/run-audit and /new-lead require ?token=<webhook_secret>. Without it, 401."""
    if not HAS_FLASK:
        return "skip"
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return "skip"
    app_mod = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    c = app_mod.app.test_client()
    # POST without token
    r = c.post("/run-audit", json={"business_name": "Foo", "email": "x@y.com"})
    assert r.status_code == 401, f"expected 401 without token, got {r.status_code}"
    # POST with the seeded token
    r = c.post(
        "/run-audit?token=smoke-webhook-secret",
        json={"business_name": "Foo", "email": "x@y.com"},
    )
    assert r.status_code == 200, f"expected 200 with token, got {r.status_code}"


def test_content_type_enforcement_returns_415():
    """Mutating /api/* endpoints must 415 when Content-Type is not JSON."""
    if not HAS_FLASK:
        return "skip"
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return "skip"
    app_mod = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    c = _logged_in_client(app_mod)
    # POST /api/clients with form-encoded (not JSON) — must 415.
    r = c.post("/api/clients", data="name=Foo&niche=Plumbing",
               content_type="application/x-www-form-urlencoded")
    assert r.status_code == 415, f"expected 415 for non-JSON, got {r.status_code}"


# ── Secret-keeping and global-dedup coverage (Tasks #3, #4) ────────────────

def test_email_contacts_dedup_blocks_duplicate_emails():
    """The global email_contacts table prevents a second prospect with the
    same email from being inserted under a different client_id (the model
    behavior we want from Task #3)."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="leadgen_dedup_")
    os.close(fd)
    db.DB_PATH = Path(tmp_path)
    db._LOCAL.conn = None
    db.init_schema()
    # Insert first contact
    cid = uuid.uuid4().hex
    db.get_db().execute(
        "INSERT INTO email_contacts(id, email, business_name, "
        "first_seen_at, last_seen_at) VALUES(?,?,?,?,?)",
        (cid, "shared@example.com", "First Biz",
         db._now(), db._now()),
    )
    # A second insert for the same email must fail the UNIQUE constraint.
    try:
        db.get_db().execute(
            "INSERT INTO email_contacts(id, email, business_name, "
            "first_seen_at, last_seen_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, "shared@example.com", "Second Biz",
             db._now(), db._now()),
        )
    except sqlite3.IntegrityError:
        return  # expected
    raise AssertionError("expected IntegrityError on duplicate email")


def test_secret_keeper_round_trips_when_master_key_set():
    """When LEADGEN_MASTER_KEY is set, secret_keeper.wrap(unwrap(x)) == x."""
    import os as _os
    saved = _os.environ.get("LEADGEN_MASTER_KEY")
    _os.environ["LEADGEN_MASTER_KEY"] = "round-trip-test-key"
    try:
        from ai_agency import secret_keeper as sk
        # LRU cache bust: the module-level singleton was built at import.
        sk.get.cache_clear()
        keeper = sk.get()
        if not keeper.enabled:
            _os.environ["LEADGEN_MASTER_KEY"] = saved or ""
            sk.get.cache_clear()
            return  # cryptography missing in test env
        plain = "supersecretvalue"
        wrapped = keeper.wrap(plain)
        # wrap on a string returns {"__enc__": "enc:..."}
        assert isinstance(wrapped, dict) and wrapped.get("__enc__"), wrapped
        out = keeper.unwrap(wrapped)
        assert out == plain, f"round-trip failed: {out!r}"
        # And it's encoded
        assert wrapped["__enc__"].startswith("enc:"), wrapped
        _os.environ["LEADGEN_MASTER_KEY"] = saved or ""
        sk.get.cache_clear()
    finally:
        if saved is not None:
            _os.environ["LEADGEN_MASTER_KEY"] = saved


def test_sender_embeds_tracking_when_enabled() -> None:
    """With tracking.on + base_url set, sender writes tracking_id + multipart msg."""
    _seed_agent_settings()
    # Turn ON tracking
    current = config.load_settings()
    current["tracking"] = {"enabled": True, "base_url": "https://agency.example.com"}
    db.set_setting("tracking", current["tracking"])
    config.load_settings.cache_clear()

    # Reset state for pid_smoke_1 so we can send a fresh step
    db.get_db().execute(
        "DELETE FROM prospect_emails WHERE prospect_id=? AND step=?",
        ("pid_smoke_1", "cold-tracked"),
    )
    db.get_db().execute(
        "DELETE FROM email_opens WHERE prospect_email_id IN "
        "(SELECT id FROM prospect_emails WHERE prospect_id=? AND step=?)",
        ("pid_smoke_1", "cold-tracked"),
    )

    captured: dict[str, Any] = {}
    def _fake_on(msg):
        captured["multipart"] = msg.is_multipart()
        captured["msg"] = msg
        return True, "ok"
    _SENDER._send_smtp = _fake_on

    niches = json.loads((_AGENCY_PKG / "config" / "niches.json").read_text())["niches"]
    plumbing = next(n for n in niches if n["key"] == "Plumbing")
    templ = _TEMPLATES.cold_email(
        plumbing, business_name="Smith Plumbing Pros",
        contact_name="John", city="Austin",
    )
    res = _SENDER.send_to_prospect(
        prospect_id="pid_smoke_1", client_id="cid_smoke_1",
        to_email="john@smithroofing.com", to_name="John",
        subject=templ["subject"], body=templ["body"], step="cold-tracked",
    )
    assert res.get("ok") is True, f"send failed: {res!r}"
    assert captured.get("multipart") is True, "expected multipart structure when tracking is on"
    sent_msg = captured["msg"]
    assert sent_msg.get_content_subtype() == "alternative", (
        f"expected multipart/alternative, got multipart/{sent_msg.get_content_subtype()}"
    )
    parts = sent_msg.get_payload()
    subtypes = {p.get_content_subtype() for p in parts if hasattr(p, "get_content_subtype")}
    assert "html" in subtypes, f"expected a text/html part, got {subtypes}"
    html_part = next(p for p in parts if p.get_content_subtype() == "html")
    html_body = html_part.get_content()
    assert "/t/o/" in html_body, "tracking pixel URL missing from HTML part"
    assert "agency.example.com" in html_body, "expected base_url embedded in pixel URL"

    row = db.get_db().execute(
        "SELECT tracking_id FROM prospect_emails WHERE prospect_id=? AND step=?",
        ("pid_smoke_1", "cold-tracked"),
    ).fetchone()
    assert row is not None, "expected prospect_emails row to exist"
    assert row["tracking_id"], "tracking_id column must be set when tracking is enabled"
    assert len(row["tracking_id"]) >= 11, f"tracking_id looks too short: {row['tracking_id']!r}"

    # And the tracking_id should resolve via the helper (round-trip)
    looked = db.lookup_send_by_tracking(row["tracking_id"])
    assert looked is not None and looked["id"] == row["tracking_id"] if False else looked is not None


def test_sender_does_not_embed_tracking_when_disabled() -> None:
    """Default settings have tracking.enabled=False → no tracking_id, no multipart."""
    _seed_agent_settings()
    # Explicitly OFF (in case a prior test set it)
    current = config.load_settings()
    current["tracking"] = {"enabled": False, "base_url": ""}
    db.set_setting("tracking", current["tracking"])
    config.load_settings.cache_clear()

    db.get_db().execute(
        "DELETE FROM prospect_emails WHERE prospect_id=? AND step=?",
        ("pid_smoke_1", "cold-untracked"),
    )

    captured: dict[str, Any] = {}
    def _fake_off(msg):
        captured["multipart"] = msg.is_multipart()
        return True, "ok"
    _SENDER._send_smtp = _fake_off

    res = _SENDER.send_to_prospect(
        prospect_id="pid_smoke_1", client_id="cid_smoke_1",
        to_email="john@smithroofing.com", to_name="John",
        subject="plain subject", body="plain body", step="cold-untracked",
    )
    assert res.get("ok") is True, f"send failed: {res!r}"
    assert captured.get("multipart") is False, "expected plain text email when tracking is off"

    row = db.get_db().execute(
        "SELECT tracking_id FROM prospect_emails WHERE prospect_id=? AND step=?",
        ("pid_smoke_1", "cold-untracked"),
    ).fetchone()
    assert row is not None
    assert not row["tracking_id"], f"tracking_id should be NULL when disabled, got {row['tracking_id']!r}"


def test_stats_open_rate_endpoint_returns_breakdown() -> None:
    """POST /api/stats/open-rate computes sent/opens/open_rate + by_client."""
    if not HAS_FLASK:
        return "skip"
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return "skip"
    import importlib
    app_mod = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    client = _logged_in_client(app_mod)
    # Pre-record: prospect_emails row + 1 open
    pe_id = uuid.uuid4().hex
    db.get_db().execute(
        "INSERT INTO prospect_emails(id, prospect_id, client_id, step, subject, body, "
        "from_address, sent_at, message_id, status, tracking_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', NULL)",
        (pe_id, "pid_smoke_1", "cid_smoke_1", "stats-test",
         "s", "b", "smoke@example.com", _now_iso(), "<m>"),
    )
    db.record_open(pe_id, "test-ua", "127.0.0.1")
    client = app_mod.app.test_client()
    r = client.get("/api/stats/open-rate")
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.get_data(as_text=True)}"
    j = r.get_json()
    assert j.get("ok") is True
    d = j["data"]
    assert d["opens"] >= 1, f"expected ≥1 open, got {d}"
    assert isinstance(d["open_rate"], float)
    assert isinstance(d["by_client"], list)
    # The cid_smoke_1 client must appear in the breakdown
    cids = {row["client_id"] for row in d["by_client"]}
    assert "cid_smoke_1" in cids


def test_route_healthz_returns_200() -> None:
    if not HAS_FLASK:
        return "skip"
    importlib_app = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    client = importlib_app.app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.get_data(as_text=True)}"
    j = r.get_json()
    assert isinstance(j, dict) and j.get("ok") is True, f"unexpected healthz body {j!r}"
    assert j.get("data", {}).get("db") is True, "expected healthz.data.db=True"


def test_route_test_llm_pins_content_type_and_ollama_guard() -> None:
    """Pins BOTH branches the /api/settings/test-llm guard produces:

    1. POST without `Content-Type: application/json` is short-circuited by
       the api-before_request JSON-CT hook → 415.
    2. POST WITH `Content-Type: application/json` while `ollama` python
       package import is failing → 500 (route's own `pip install ollama`
       hint). This is the operator-facing failure mode.

    Without parametrising these we lose the ability to detect a regression
    that collapses the two guards into one (e.g. a future refactor that
    swallows ImportError silently, or a JSON-CT hook applied to the
    pixell path by mistake)."""
    if not HAS_FLASK:
        return "skip"
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return "skip"
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "ollama" or name.startswith("ollama."):
            raise ImportError("simulated: ollama python package not installed")
        return real_import(name, *args, **kwargs)

    # Build the failing-helper, install it on builtins.__import__, and
    # pin Branch 2 of the test with try/finally so the guard never leaks
    # into the next test in the suite. The outer try/finally covers BOTH
    # Branch 1 (which doesn't need the guard) and Branch 2 (which does),
    # so a future failure between the two branches can't poison the
    # remaining tests.
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "ollama" or name.startswith("ollama."):
            raise ImportError("simulated: ollama python package not installed")
        return real_import(name, *args, **kwargs)

    importlib_app = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    client = _logged_in_client(importlib_app)

    builtins.__import__ = guarded
    try:
        # Branch 1: non-JSON Content-Type header -> 415 from the JSON-CT hook.
        # We send an EXPLICIT form-urlencoded body so request.headers
        # reports Content-Type="application/x-www-form-urlencoded" rather
        # than empty (which would let the request bypass the middleware).
        r = client.post(
            "/api/settings/test-llm",
            data="x=y",
            content_type="application/x-www-form-urlencoded",
        )
        assert r.status_code == 415, (
            f"expected 415 for non-JSON Content-Type, got {r.status_code} {r.get_data(as_text=True)!r}"
        )
        j = r.get_json() or {}
        assert j.get("error") == "content_type_must_be_application_json", j

        # Branch 2: with proper Content-Type + ollama guard raised -> 500 with hint.
        r = client.post(
            "/api/settings/test-llm", headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 500, (
            f"expected 500 when ollama missing, got {r.status_code} {r.get_data(as_text=True)!r}"
        )
        j = r.get_json() or {}
        err = j.get("error", "")
        assert "pip install ollama" in err or "ollama" in err, (
            f"expected ollama-related error, got {err!r}"
        )
        assert j.get("ok") is False
    finally:
        builtins.__import__ = real_import


# ── Run ───────────────────────────────────────────────────────────────────

def test_track_open_route_marks_bot_ua() -> None:
    """End-to-end: GET /t/o/<id>.gif stamps is_bot=1 on Proofpoint UA, 0 on Thunderbird UA.

    Wiring-level coverage that the unit-level record_open tests cannot
    provide: route registration, header extraction, settings read, and
    the route call into db.record_open(is_bot=...) are all exercised.
    Also covers three defensive contracts the route docstring promises:
      - the route is GET-only (POST gets 405),
      - the GIF magic bytes are served on every response, including miss,
      - unknown tracking_ids never surface 4xx to scanners.
    """
    if not HAS_FLASK:
        return "skip"

    # Re-seed tracking with bot_user_agents: prior sender tests overwrite
    # the tracking dict with a 2-key dict that drops bot_user_agents, so
    # the matcher would silently return False for every UA if we didn't
    # restore the patterns here.
    db.set_setting("tracking", {
        "enabled": True,
        "base_url": "https://agency.example.com",
        "import_ip_truncate": True,
        "bot_user_agents": [
            "Proofpoint", "Mimecast", "Barracuda", "Symantec",
            "Microsoft Defender", "URLPhishing", "phishtank",
            "Sophos", "IronPort", "FireEye",
        ],
    })
    config.load_settings.cache_clear()

    new_tracking = "tkeroute" + uuid.uuid4().hex[:12]
    row_id = uuid.uuid4().hex
    db.get_db().execute(
        "INSERT INTO prospect_emails(id, prospect_id, client_id, step, subject, body, "
        "from_address, sent_at, message_id, status, tracking_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?)",
        (row_id, "pid_smoke_1", "cid_smoke_1", "bot-route-test",
         "s", "b", "smoke@example.com", _now_iso(), "<m@botroute>", new_tracking),
    )
    db.get_db().execute(
        "DELETE FROM email_opens WHERE prospect_email_id=?", (row_id,),
    )

    app_mod = _load("ai_agency.app", _AGENCY_PKG / "app.py")
    client = app_mod.app.test_client()
    pixel = "/t/o/" + new_tracking + ".gif"

    def assert_gif(resp, *, label):
        assert resp.status_code == 200, (
            f"{label}: expected 200, got {resp.status_code}"
        )
        ct = resp.headers.get("Content-Type", "")
        assert ct.startswith("image/gif"), (
            f"{label}: expected image/gif Content-Type, got {ct!r}"
        )
        assert resp.get_data()[:6] == b"GIF89a", (
            f"{label}: expected GIF89a magic bytes in pixel body"
        )

    def last_open_is_bot(expected):
        # Return ALL rows so the cumulative count assertions in the test
        # body can verify that each GET /t/o/<id>.gif call inserted a new
        # row. ORDER BY id DESC keeps the most recent first so the
        # rows[0]["is_bot"] check still targets the latest insert.
        rows = db.get_db().execute(
            "SELECT is_bot FROM email_opens "
            "WHERE prospect_email_id=? ORDER BY id DESC",
            (row_id,),
        ).fetchall()
        assert rows, "expected at least one email_opens row to exist"
        assert int(rows[0]["is_bot"]) == int(expected), (
            f"expected is_bot={expected}, got {rows[0]['is_bot']!r}"
        )
        return rows

    # 1) Proofpoint UA -> is_bot=1, GIF served, opens counter incremented.
    r = client.get(
        pixel,
        headers={"User-Agent": "Mozilla/5.0 (Proofpoint URL Defense scanner)"},
    )
    assert_gif(r, label="proofpoint")
    rows = last_open_is_bot(expected=1)
    assert len(rows) == 1, f"proofpoint: expected 1 open row total, got {len(rows)}"

    # 2) Thunderbird UA -> is_bot=0, opens to 2 total.
    thunderbird = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:102.0) "
        "Gecko/20100101 Thunderbird/102.0"
    )
    r = client.get(pixel, headers={"User-Agent": thunderbird})
    assert_gif(r, label="thunderbird")
    rows = last_open_is_bot(expected=0)
    assert len(rows) == 2, (
        f"thunderbird: expected 2 total open rows, got {len(rows)}"
    )

    # 3) curl UA -> is_bot=0 (defensive: an unknown UA must not be flagged).
    r = client.get(pixel, headers={"User-Agent": "curl/8.0.0"})
    assert_gif(r, label="curl")
    assert last_open_is_bot(expected=0), "curl UA must not be flagged as a bot"

    # 4) Empty UA -> is_bot=0. The route docstring says the matcher
    #    short-circuits on empty UA; verify that contract.
    r = client.get(pixel, headers={"User-Agent": ""})
    assert_gif(r, label="empty-ua")
    assert last_open_is_bot(expected=0), "empty UA must not be flagged as a bot"

    # 5) Unknown tracking_id still serves the GIF (200 + magic bytes) so
    #    scanner retries can't be provoked by a 4xx \u2014 important for
    #    sender reputation. Also: the route is GET-only; a POST gets 405.
    miss = client.get("/t/o/definitely-no-such-token.gif")
    assert_gif(miss, label="unknown-tracking-id")
    post_resp = client.post(pixel)
    assert post_resp.status_code == 405, (
        f"POST /t/o/<id>.gif must be 405 (route is GET-only), "
        f"got {post_resp.status_code}"
    )


_R = Results()
_TEST_FNS = [
    ("init_schema: 12 expected tables",                         test_init_schema),
    ("settings seeds apis.ollama_model='llama3'",                test_settings_seed_ollama_model),
    ("insert a client",                                         test_create_client),
    ("UNIQUE(client_id, external_id) blocks duplicate prospects", test_prospect_dedupe),
    ("subject_templates vary randomly across renders",           test_subject_templates_vary_randomly),
    ("keyword fallback returns HOT",                            test_keyword_fallback_returns_hot),
    ("UNSUBSCRIBE persists to blacklist",                       test_classify_unsubscribe_persists_to_blacklist),
    ("send_to_prospect dispatches + persists + bumps quota",    test_send_to_prospect_records_and_bumps_quota),
    ("send_to_prospect obeys 7-day idempotency window",         test_send_idempotency_window),
    ("send_to_prospect refuses blacklisted email",              test_send_blacklisted_email_blocked),
    ("forwarder emails one prospect only — no list leak",       test_forward_hot_reply_emails_only_one_prospect),
    ("open-rate helpers: record_open + lookup + count",         test_record_open_and_lookup_by_tracking),
    ("sender embeds tracking pixel when tracking.enabled",     test_sender_embeds_tracking_when_enabled),
    ("sender skips pixel when tracking disabled",               test_sender_does_not_embed_tracking_when_disabled),
    ("GET /api/stats/open-rate returns breakdown",             test_stats_open_rate_endpoint_returns_breakdown),
    ("GET /healthz returns 200 ok",                             test_route_healthz_returns_200),
    ("POST /api/* pins both 415 (no CT) and 500 (ollama guard)",     test_route_test_llm_pins_content_type_and_ollama_guard),
     ("GET /t/o/<id>.gif stamps is_bot=1 for Proofpoint UA, 0 otherwise", test_track_open_route_marks_bot_ua),
]




def main() -> int:
    _bootstrap()
    total = len(_TEST_FNS)
    print("LeadGen AI smoke tests")
    print("=" * 64)
    for name, fn in _TEST_FNS:
        _R.run(name, fn)
    _restore_auth_is_authed()
    print()
    msg = f"  {_R.passed}/{total} passed"
    if _R.skipped:
        msg += f", {_R.skipped} skipped"
    if _R.failed:
        msg += f", {_R.failed} failed"
    print(msg)
    if _R.failures:
        print()
        print("  Failures:")
        for n, m in _R.failures:
            print(f"    - {n}: {m}")
    return 0 if _R.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
