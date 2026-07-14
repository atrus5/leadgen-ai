"""
SMTP sender. Wraps `smtplib` with rate-limiting and per-from-day quota
enforcement using `sender_quotas` in the DB.

If the prospect already has a recent `prospect_emails` row matching this step
in the last 7 days, we refuse to send (idempotency + bounce protection).
"""
from __future__ import annotations

import html as _html
import logging
import secrets as _secrets
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from .. import config, db

LOG = logging.getLogger("outreach.sender")


def _agent_settings() -> dict[str, Any]:
    return config.load_settings().get("agent", {})


def _text_to_html(text: str) -> str:
    """Best-effort text→HTML for the multipart/alternative HTML body. Avoids
    passing user-controlled chars un-escaped (works for our generated copy
    which is plain English sentences, with no angle brackets)."""
    return "<p>" + _html.escape(text or "").replace("\n", "<br>") + "</p>"


def _build_message(*, from_addr: str, from_name: str, to_email: str, subject: str, body: str, reply_to: str | None = None, tracking_url: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    full_from = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["From"] = full_from
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@", 1)[-1])
    if reply_to:
        msg["Reply-To"] = reply_to
    if tracking_url:
        # Multipart/alternative: keep the plain-text body for compatibility
        # with text-only clients, and add an HTML version carrying the
        # invisible 1x1 tracking pixel.
        msg.set_content(body)
        html_body = _text_to_html(body) + (
            f'<img src="{_html.escape(tracking_url)}" width="1" height="1" alt="" '
            f'style="display:none;border:0;outline:none" />'
        )
        msg.add_alternative(html_body, subtype="html")
    else:
        msg.set_content(body)
    return msg


def quota_remaining(from_addr: str) -> int:
    s = _agent_settings()
    cap = int((config.load_settings().get("warmup", {}) or {}).get("max_daily_after") or 40)
    used = db.quota_today(from_addr)
    return max(0, cap - used)


def _send_smtp(msg: EmailMessage) -> tuple[bool, str]:
    s = _agent_settings()
    host = s.get("smtp_host")
    user = s.get("smtp_user")
    pw = s.get("smtp_pass")
    if not (host and user and pw):
        return False, "smtp_credentials_missing"
    port = int(s.get("smtp_port") or 587)
    tls_mode = (s.get("smtp_tls") or "starttls").lower()
    try:
        if port == 465 or tls_mode == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as smtp:
                smtp.ehlo()
                smtp.login(user, pw)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                if tls_mode in ("starttls", "tls"):
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                smtp.login(user, pw)
                smtp.send_message(msg)
        return True, "ok"
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        return False, f"smtp_refused:{exc}"
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"smtp_auth:{exc}"
    except Exception as exc:
        LOG.exception("smtp send failed")
        return False, f"smtp_exception:{exc}"


def send_to_prospect(
    *,
    prospect_id: str,
    client_id: str | None,
    to_email: str,
    to_name: str | None,
    subject: str,
    body: str,
    step: str,
) -> dict[str, Any]:
    """
    Send one outbound email. Idempotent on (prospect_id, step) within 7 days;
    capped by sender_quotas; logged to prospect_emails regardless of outcome.
    """
    s = _agent_settings()
    from_addr = s.get("from_address") or s.get("smtp_user") or ""
    from_name = s.get("from_name") or ""
    if not from_addr:
        return {"ok": False, "error": "from_address_unset", "prospect_id": prospect_id}

    if db.blacklist_contains(to_email):
        return {"ok": False, "error": "blacklisted", "prospect_id": prospect_id}

    if quota_remaining(from_addr) <= 0:
        db.audit("send.quota_exceeded", {"prospect_id": prospect_id, "from": from_addr})
        return {"ok": False, "error": "daily_quota_exceeded", "prospect_id": prospect_id}

    # Idempotency check — refuse to re-send the same step within a
    # 7-day window, so re-entering the same prospect next quarter still
    # gets a real cold-email send rather than a stale blocker.
    conn = db.get_db()
    recent = conn.execute(
        "SELECT sent_at FROM prospect_emails "
        "WHERE prospect_id=? AND step=? "
        "AND sent_at > datetime('now','-7 day') "
        "ORDER BY sent_at DESC LIMIT 1",
        (prospect_id, step),
    ).fetchone()
    if recent:
        return {
            "ok": False,
            "error": "duplicate_step",
            "prospect_id": prospect_id,
            "last_sent_at": recent["sent_at"],
            "note": "Same step already sent within last 7 days",
        }

    # Tracking pixel: only when both `enabled=True` AND `base_url` set.
    tracking_cfg = (config.load_settings().get("tracking") or {})
    tracking_id: str | None = None
    tracking_url: str | None = None
    if tracking_cfg.get("enabled") and (tracking_cfg.get("base_url") or "").strip():
        tracking_id = _secrets.token_urlsafe(8)  # 11-char URL-safe token
        tracking_url = f"{tracking_cfg['base_url'].rstrip('/')}/t/o/{db.current_workspace()}/{tracking_id}.gif"

    msg = _build_message(
        from_addr=from_addr,
        from_name=from_name,
        to_email=to_email,
        subject=subject,
        body=body,
        reply_to=from_addr,
        tracking_url=tracking_url,
    )
    ok, status = _send_smtp(msg)
    if ok:
        db.quota_increment(from_addr)

    # Generate id in Python — cleaner than an inline randomblob subquery
    # (SQLite accepts scalar subqueries in VALUES, but the paren-counting
    # is easy to get wrong and silently produces a syntax error).
    new_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO prospect_emails(id, prospect_id, client_id, step, subject, body, "
        "from_address, sent_at, message_id, status, tracking_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id,
            prospect_id, client_id, step, subject, body,
            from_addr, db._now(), msg["Message-ID"],
            "sent" if ok else f"failed:{status}",
            tracking_id,
        ),
    )
    if ok:
        # Only update last_contacted_at + status when the SMTP server
        # actually accepted the message — otherwise follow-up logic
        # thinks the prospect was touched yesterday when they weren't.
        conn.execute(
            "UPDATE prospects SET last_contacted_at=? WHERE id=?",
            (db._now(), prospect_id),
        )
        conn.execute("UPDATE prospects SET status='contacted' WHERE id=? AND status='new'", (prospect_id,))
        db.audit("send.ok", {"prospect_id": prospect_id, "step": step})
    db.audit("send.attempt", {"prospect_id": prospect_id, "step": step, "ok": ok, "status": status})
    return {"ok": ok, "status": status, "prospect_id": prospect_id}
