"""
IMAP reply poller.

Periodically connects to the configured IMAP mailbox over SSL, fetches UNSEEN
messages, attempts to match each sender against our prospects table, and
classifies the intent (HOT / MORE_INFO / NOT_NOW / OUT_OF_OFFICE / UNSUBSCRIBE
/ NOISE) using Llama 3 via Ollama (or a deterministic keyword fallback when
the local LLM is unreachable).

HOT-classified rows are forwarded to the agency client by
`outreach/forwarder.py`. ALL repliess are persisted in `replies`, even NOISE,
so the dashboard sees real numbers.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import uuid
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Any, Iterable

from .. import config, db

LOG = logging.getLogger("outreach.reply_parser")

LABELS = ("HOT", "MORE_INFO", "NOT_NOW", "OUT_OF_OFFICE", "UNSUBSCRIBE", "NOISE")
UNSUBSCRIBE_PATTERN = re.compile(r"\b(unsubscribe|stop|remove me|opt[-\s]?out)\b", re.IGNORECASE)
OOO_PATTERN = re.compile(r"\b(out of (the )?office|away from|on vacation|returning on|auto[-\s]?reply)\b", re.IGNORECASE)


def _agent() -> dict[str, Any]:
    return config.load_settings().get("agent", {})


def _classify_with_llama(subject: str, body: str) -> tuple[str | None, float | None]:
    """Returns (label, confidence) or (None, None) if LLM unavailable."""
    try:
        import ollama  # type: ignore
        system = (
            "You are an intent classifier for cold-email replies. "
            "Reply with EXACTLY one of these tokens (no explanation, no quotes):\n"
            "HOT  - clear interest, wants a call, quote, meeting, or follow-up info\n"
            "MORE_INFO  - asks for details or onboarding steps but not yet ready to buy\n"
            "NOT_NOW  - polite deferral ('not now', 'maybe later', 'next quarter')\n"
            "OUT_OF_OFFICE  - automatic or vacation reply\n"
            "UNSUBSCRIBE  - angry / stop / remove me / never email again\n"
            "NOISE  - unrelated promotional, or corporate noise\n"
        )
        prompt = f"Subject: {subject or '(none)'}\n\nBody:\n{body[:2000]}"
        r = ollama.chat(model="llama3", messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ])
        text = (r.get("message") or {}).get("content") or ""
        text = text.strip().upper()
        for label in LABELS:
            if label in text:
                return label, 0.85
    except Exception as exc:
        LOG.debug("llama classifier offline: %s", exc)
    return None, None


def _classify_with_keywords(subject: str, body: str) -> tuple[str, float]:
    text = f"{subject}\n{body}".lower()
    if OOO_PATTERN.search(text):
        return "OUT_OF_OFFICE", 0.9
    if UNSUBSCRIBE_PATTERN.search(text):
        return "UNSUBSCRIBE", 0.95
    if any(k in text for k in (
        "interested", "tell me more", "send me", "yes, please", "set up a call",
        "let's chat", "let's talk", "schedule", "book a", "quote",
        "can you call", "more information", "send details",
    )):
        return "HOT", 0.65
    if any(k in text for k in (
        "not now", "next quarter", "later", "bad time", "maybe next",
    )):
        return "NOT_NOW", 0.6
    return "NOISE", 0.4


def classify(subject: str, body: str) -> tuple[str, float]:
    label, conf = _classify_with_llama(subject, body)
    if label is None:
        label, conf = _classify_with_keywords(subject, body)
    return label, conf


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value or ""


def _imap_connect() -> imaplib.IMAP4 | imaplib.IMAP4_SSL | None:
    a = _agent()
    host = a.get("imap_host")
    user = a.get("imap_user")
    pw = a.get("imap_pass")
    if not (host and user and pw):
        return None
    port = int(a.get("imap_port") or 993)
    if a.get("imap_ssl", True):
        return imaplib.IMAP4_SSL(host, port)
    return imaplib.IMAP4(host, port)


def fetch_unseen(limit: int = 25) -> list[dict[str, Any]]:
    """Connect, fetch up to `limit` UNSEEN messages, return normalised dicts."""
    conn = _imap_connect()
    if conn is None:
        return []
    try:
        user = _agent()["imap_user"]
        pw = _agent()["imap_pass"]
        conn.login(user, pw)
        conn.select("INBOX")
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[:limit]
        results: list[dict[str, Any]] = []
        for mid in ids:
            typ, msgdata = conn.fetch(mid, "(RFC822)")
            if typ != "OK":
                continue
            raw_bytes = msgdata[0][1]
            msg = email.message_from_bytes(raw_bytes)
            subject = _decode(msg.get("Subject"))
            from_field = _decode(msg.get("From"))
            _, from_email = parseaddr(from_field)
            body = _extract_body(msg)
            results.append({
                "from_email": from_email.lower(),
                "from_display": from_field,
                "subject": subject,
                "body": body,
                "raw_headers": _decode(msg.get("All-Headers") or ""),
            })
            # Mark as Seen so we don't re-classify on the next 15-min poll.
            try:
                conn.store(mid, "+FLAGS", "\\Seen")
            except Exception as exc:
                LOG.debug("imap store SEEN failed for %s: %s", mid, exc)
        return results
    except Exception as exc:
        LOG.exception("imap fetch failed")
        db.audit("imap.fetch_failed", {"err": str(exc)})
        return []
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _extract_body(msg) -> str:
    """Walk the email parts and prefer text/plain. Take first 4 kB."""
    chunks: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain":
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            except Exception:
                continue
        elif ctype == "text/html" and not chunks:
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    txt = re.sub(r"<[^>]+>", "", payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
                    chunks.append(txt)
            except Exception:
                continue
    text = "\n".join(chunks).strip()
    return text[:4000]


def persist_and_classify(reply: dict[str, Any]) -> dict[str, Any]:
    """Match the sender to a prospect, classify, persist, return row data."""
    label, conf = classify(reply.get("subject", ""), reply.get("body", ""))
    conn = db.get_db()
    prospect_row = conn.execute(
        "SELECT id, client_id, business_name FROM prospects WHERE lower(contact_email)=? LIMIT 1",
        (reply["from_email"],),
    ).fetchone()
    if not prospect_row:
        # Try domain match as a last resort (but do NOT auto-attach when ambiguous)
        prospect_row = None
    pid = prospect_row["id"] if prospect_row else None
    cid = prospect_row["client_id"] if prospect_row else None
    rid = db.get_db().execute(
        "INSERT INTO replies(id, prospect_id, client_id, from_address, subject, body, "
        "received_at, intent, confidence, forwarded, raw_headers) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (
            uuid.uuid4().hex,
            pid, cid, reply["from_email"], reply.get("subject", ""), reply.get("body", ""),
            db._now(), label, conf, reply.get("raw_headers", ""),
        ),
    )
    if pid and label in ("HOT", "MORE_INFO"):
        conn.execute("UPDATE prospects SET status='replied', last_replied_at=? WHERE id=?", (db._now(), pid))
    if label == "UNSUBSCRIBE":
        db.blacklist_add(reply["from_email"], "auto:reply_parser")
    db.audit("reply.classified", {"from": reply["from_email"], "label": label, "prospect_id": pid})
    return {
        "id": rid,
        "label": label,
        "confidence": conf,
        "prospect_id": pid,
        "client_id": cid,
    }
