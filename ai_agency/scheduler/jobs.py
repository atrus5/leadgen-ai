"""
APScheduler wiring for four background jobs:

  1. nightly_hunt      - every night, run Apollo + Google Places for each
                         active client and persist new prospects
  2. morning_brief      - email the operator the daily summary of new
                         prospects that were found
  3. reply_poller       - every 15 min, fetch UNSEEN IMAP replies and
                         classify / persist / forward HOT replies
  4. warmup_tick        - once per day, increment the per-from daily send
                         counter and mark warmups complete at day 14

All jobs use `job_runs` for restart-safety: each scheduled tick writes a row
before doing work, and queries `job_runs` to avoid double-firing if the
process was restarted within the same window.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from .. import config, db
from ..hunters import apollo, google_places
from ..outreach import forwarder, reply_parser, sender

LOG = logging.getLogger("scheduler.jobs")

_scheduler: BackgroundScheduler | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _day_label(d: datetime | None = None) -> str:
    d = d or datetime.now(timezone.utc)
    return d.isoformat(timespec="seconds")


def _already_ran_today(job: str) -> bool:
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    row = db.get_db().execute(
        "SELECT id FROM job_runs WHERE job=? AND started_at>=? LIMIT 1",
        (job, today_start),
    ).fetchone()
    return row is not None


def _record_run_start(job: str) -> int:
    """
    Insert the job_runs row. If the (job, started_at) UNIQUE constraint
    collides, a parallel tick is already running for the same second; we
    raise so the caller can abort cleanly instead of doing the work twice.
    """
    try:
        cur = db.get_db().execute(
            "INSERT INTO job_runs(job, started_at, ok) VALUES(?,?,0)",
            (job, _day_label()),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise RuntimeError(f"job {job} already started this second (concurrent tick)")


def _record_run_end(rid: int, *, ok: bool, summary: Any = None, error: str | None = None) -> None:
    db.get_db().execute(
        "UPDATE job_runs SET finished_at=?, ok=?, summary=?, error=? WHERE id=?",
        (_day_label(), 1 if ok else 0, db._safe_json(summary), error, rid),
    )


def nightly_hunt(_: datetime | None = None) -> dict[str, Any]:
    """Hunt for new prospects for every active client."""
    if _already_ran_today("nightly_hunt"):
        return {"skipped": True, "reason": "already_ran_today"}
    try:
        rid = _record_run_start("nightly_hunt")
    except RuntimeError as exc:
        return {"skipped": True, "reason": str(exc)}
    summary: dict[str, Any] = {"clients": [], "total_inserted": 0}
    try:
        clients = db.get_db().execute(
            "SELECT * FROM clients WHERE status='active' ORDER BY created_at"
        ).fetchall()
        for c in clients:
            client_dict = dict(c)
            inserted = 0
            if apollo.enabled():
                try:
                    a = apollo.run_for_client(client_dict)
                    inserted += a["inserted"]
                except ValueError as exc:
                    # Apollo needs full geo — log and skip just this client.
                    summary["clients"].append({
                        "client_id": client_dict["id"],
                        "name": client_dict["name"],
                        "niche": client_dict["niche"],
                        "inserted": 0,
                        "skipped_reason": str(exc),
                    })
                    continue
            if google_places.enabled():
                g = google_places.run_for_client(client_dict)
                inserted += g["inserted"]
            summary["clients"].append({
                "client_id": client_dict["id"],
                "name": client_dict["name"],
                "niche": client_dict["niche"],
                "inserted": inserted,
            })
            summary["total_inserted"] += inserted
        _record_run_end(rid, ok=True, summary=summary)
        return summary
    except Exception as exc:
        _record_run_end(rid, ok=False, error=str(exc))
        LOG.exception("nightly_hunt failed")
        return {"ok": False, "error": str(exc)}


def morning_brief(_: datetime | None = None) -> dict[str, Any]:
    """Email operator the daily summary.

    Uses the configured morning_brief_to address; falls back to owner_email.
    Skips silently if neither is configured or if SMTP creds unset.
    """
    if _already_ran_today("morning_brief"):
        return {"skipped": True, "reason": "already_ran_today"}
    rid = _record_run_start("morning_brief")
    try:
        s = config.load_settings().get("agent", {})
        to = s.get("morning_brief_to") or s.get("owner_email")
        if not to:
            _record_run_end(rid, ok=False, error="brief_email_unset")
            return {"ok": False, "error": "brief_email_unset"}
        # Pull today's selection from hunt_runs
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = db.get_db().execute(
            "SELECT hr.client_id, hr.started_at, hr.found_count, hr.inserted_count, "
            "       hr.source, c.name AS client_name, c.niche "
            "FROM hunt_runs hr LEFT JOIN clients c ON hr.client_id=c.id "
            "WHERE hr.started_at>=? ORDER BY hr.started_at",
            (today_start.isoformat(timespec="seconds"),),
        ).fetchall()
        subject = f"Morning brief — {today_start.strftime('%a %b %d')}"
        if not rows:
            subject = subject + " (no new prospects)"
        body = _format_brief(rows)
        from_addr = s.get("smtp_user") or s.get("from_address")
        if not (s.get("smtp_host") and s.get("smtp_user") and s.get("smtp_pass") and from_addr):
            _record_run_end(rid, ok=False, error="smtp_credentials_missing")
            return {"ok": False, "error": "smtp_credentials_missing"}
        result = _send_simple(from_addr, to, subject, body)
        _record_run_end(rid, ok=result["ok"], summary={"to": to, "subject": subject}, error=result.get("error"))
        return result
    except Exception as exc:
        _record_run_end(rid, ok=False, error=str(exc))
        LOG.exception("morning_brief failed")
        return {"ok": False, "error": str(exc)}


def _format_brief(rows) -> str:
    head = [
        "Good morning,",
        "",
        "Here's what the nightly hunt turned up.",
        "",
    ]
    if not rows:
        return "\n".join(head + [
            "Nothing new overnight — either all clients paused, the API keys are unset,",
            "or the hunters hit their quotas. Check the scheduler dashboard.",
        ])
    total_inserted = sum(r["inserted_count"] or 0 for r in rows)
    lines = head + [f"Total new prospects inserted: {total_inserted}", ""]
    for r in rows:
        lines.append(f"• {r['client_name'] or '(unlinked)'} ({r['niche'] or '—'}) via {r['source']}: "
                     f"{r['inserted_count'] or 0}/{r['found_count'] or 0} inserted")
    lines += [
        "",
        "Open LeadGen AI to review each prospect, generate a niche-tuned cold email,",
        "and send it (the system will respect your warmup daily quotas).",
        "",
        "— LeadGen AI",
    ]
    return "\n".join(lines)


def _send_simple(from_addr: str, to_addr: str, subject: str, body: str) -> dict[str, Any]:
    """Send a single transactional message using the configured SMTP."""
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid
    import smtplib, ssl

    s = config.load_settings().get("agent", {})
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@", 1)[-1])
    msg.set_content(body)
    host = s["smtp_host"]
    port = int(s.get("smtp_port") or 587)
    tls_mode = (s.get("smtp_tls") or "starttls").lower()
    try:
        if port == 465 or tls_mode == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as sm:
                sm.login(s["smtp_user"], s["smtp_pass"])
                sm.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as sm:
                sm.ehlo()
                if tls_mode in ("starttls", "tls"):
                    sm.starttls(context=ssl.create_default_context())
                    sm.ehlo()
                sm.login(s["smtp_user"], s["smtp_pass"])
                sm.send_message(msg)
        return {"ok": True, "to": to_addr, "subject": subject}
    except Exception as exc:
        LOG.exception("morning_brief send failed")
        return {"ok": False, "error": str(exc)}


def reply_poller(_: datetime | None = None) -> dict[str, Any]:
    """Poll IMAP for UNSEEN replies, classify, persist, forward HOTs."""
    s = config.load_settings().get("agent", {})
    if not (s.get("imap_host") and s.get("imap_user") and s.get("imap_pass")):
        return {"skipped": True, "reason": "imap_unconfigured"}
    rid = _record_run_start("reply_poller")
    try:
        replies = reply_parser.fetch_unseen(limit=25)
        forwarded = 0
        for reply in replies:
            rec = reply_parser.persist_and_classify(reply)
            if rec["label"] in ("HOT", "MORE_INFO"):
                r = forwarder.forward(rec["id"])
                if r.get("ok"):
                    forwarded += 1
        _record_run_end(rid, ok=True, summary={"fetched": len(replies), "forwarded": forwarded})
        return {"fetched": len(replies), "forwarded": forwarded}
    except Exception as exc:
        _record_run_end(rid, ok=False, error=str(exc))
        LOG.exception("reply_poller failed")
        return {"ok": False, "error": str(exc)}


def warmup_tick(_: datetime | None = None) -> dict[str, Any]:
    """
    Once a day: compute each warming domain's current day index, log a row in
    `warmup_log`, mark complete at day 14.
    """
    if _already_ran_today("warmup_tick"):
        return {"skipped": True, "reason": "already_ran_today"}
    rid = _record_run_start("warmup_tick")
    try:
        rows = db.get_db().execute("SELECT * FROM warmups WHERE state='warming'").fetchall()
        completed = 0
        for r in rows:
            day_idx = _day_index(r["start_date"])
            target = sender.quota_remaining(r["from_address"]) or 0
            current = db.quota_today(r["from_address"])
            db.get_db().execute(
                "INSERT INTO warmup_log(warmup_id, day, sent_count, target_count, logged_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(warmup_id, day) DO NOTHING",
                (r["id"], day_idx, current, target, _day_label()),
            )
            if day_idx >= 14 and not r["completed"]:
                db.get_db().execute(
                    "UPDATE warmups SET state='warmed', completed=1, completed_at=? WHERE id=?",
                    (_day_label(), r["id"]),
                )
                completed += 1
                db.audit("warmup.completed", {"warmup_id": r["id"]})
        _record_run_end(rid, ok=True, summary={"completed": completed})
        return {"completed": completed}
    except Exception as exc:
        _record_run_end(rid, ok=False, error=str(exc))
        LOG.exception("warmup_tick failed")
        return {"ok": False, "error": str(exc)}


def _day_index(start_iso: str) -> int:
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    return max(1, (datetime.now(timezone.utc) - start).days + 1)


# ── Boot / shutdown ──────────────────────────────────────────────────────

def install() -> BackgroundScheduler:
    """Start the scheduler. Idempotent and restart-safe."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    s = config.load_settings().get("scheduler", {}) or {}
    s_agent = config.load_settings().get("agent", {}) or {}
    if not s.get("enabled"):
        LOG.info("scheduler disabled in settings")
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.start()
        return _scheduler

    sched = BackgroundScheduler(daemon=True)
    if s.get("run_nightly_hunt"):
        sched.add_job(
            nightly_hunt, "cron",
            hour=int(s_agent.get("nightly_hunt_hour") or 2),
            minute=10,
            id="nightly_hunt", replace_existing=True, misfire_grace_time=600,
        )
    if s.get("run_morning_brief"):
        sched.add_job(
            morning_brief, "cron",
            hour=int(s_agent.get("morning_brief_hour") or 8),
            minute=5,
            id="morning_brief", replace_existing=True, misfire_grace_time=600,
        )
    if s.get("run_reply_poller"):
        sched.add_job(
            reply_poller, "interval", minutes=15,
            id="reply_poller", replace_existing=True, misfire_grace_time=300,
        )
    sched.add_job(
        warmup_tick, "cron",
        hour=23, minute=50,
        id="warmup_tick", replace_existing=True, misfire_grace_time=600,
    )

    def _log_event(event):
        if event.exception:
            LOG.exception("job %s failed: %s", event.job_id, event.exception)

    sched.add_listener(_log_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    sched.start()
    _scheduler = sched
    LOG.info("scheduler installed")
    return sched


def shutdown(wait: bool = False) -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=wait)
        _scheduler = None
