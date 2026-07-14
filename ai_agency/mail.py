"""
Minimal shared SMTP-send helper. Used by invites.py to send invite emails
through the platform mailbox. Not wired into outreach/sender.py,
outreach/forwarder.py, or scheduler/jobs.py's own send helpers — those each
have send-specific behavior (rate limiting, tracking pixels, reply
threading) worth keeping separate; this is only for simple one-off
transactional messages.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

LOG = logging.getLogger("ai_agency.mail")


def send_plain_email(smtp_cfg: dict[str, Any], from_addr: str, to_addr: str, subject: str, body: str) -> dict[str, Any]:
    """Send a single plain-text transactional message. `smtp_cfg` needs
    smtp_host/smtp_port/smtp_user/smtp_pass/smtp_tls keys (same shape as
    settings.agent). Returns {"ok": True} or {"ok": False, "error": str}."""
    host = smtp_cfg.get("smtp_host")
    user = smtp_cfg.get("smtp_user")
    pw = smtp_cfg.get("smtp_pass")
    if not (host and user and pw):
        return {"ok": False, "error": "smtp_not_configured"}
    port = int(smtp_cfg.get("smtp_port") or 587)
    tls_mode = (smtp_cfg.get("smtp_tls") or "starttls").lower()

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@", 1)[-1] if "@" in from_addr else None)
    msg.set_content(body)

    try:
        if port == 465 or tls_mode == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as sm:
                sm.login(user, pw)
                sm.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as sm:
                sm.ehlo()
                if tls_mode in ("starttls", "tls"):
                    sm.starttls(context=ssl.create_default_context())
                    sm.ehlo()
                sm.login(user, pw)
                sm.send_message(msg)
        return {"ok": True, "to": to_addr, "subject": subject}
    except Exception as exc:
        LOG.exception("send_plain_email failed")
        return {"ok": False, "error": str(exc)}
