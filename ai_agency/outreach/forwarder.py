"""
Forwards HOT-classified replies to the agency client (the one paying us for
lead-gen). The forward includes only the essentials — mainline prospect
contact info + the original message text + a short brief — and never bleeds
our internal prospect list.

The forward is sent via the same SMTP credentials used for outbound. We
logically rely on the existing `sender._send_smtp` to keep a single place for
mail transport concerns; we build the message inline so the From/Reply-To can
reflect the operator-facing narrative rather than the cold sender identity.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from .. import config, db
from ..outreach import sender as smtp_sender

LOG = logging.getLogger("outreach.forwarder")


def _build_forward_email(*, operator_from: str, client_email: str, subject: str, body: str, operator_name: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{operator_name} <{operator_from}>" if operator_name else operator_from
    msg["To"] = client_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=operator_from.split("@", 1)[-1])
    msg.set_content(body)
    return msg


def _format_body(reply: dict, prospect: dict, client: dict) -> str:
    cfg = (config.load_settings().get("hot_forward") or {})
    lines = [
        f"Hi {client.get('contact_name') or 'there'},",
        "",
        f"A prospect of yours ({prospect.get('business_name')}, "
        f"{prospect.get('city') or 'your service area'}) just raised their hand "
        "and asked for more information. The full transcript is below.",
        "",
        "— LEAD BRIEF —",
        f"Business:  {prospect.get('business_name')}",
    ]
    if prospect.get("contact_name"):
        lines.append(f"Contact:   {prospect['contact_name']}" + (f"  ({prospect['contact_title']})" if prospect.get("contact_title") else ""))
    if prospect.get("contact_email"):
        lines.append(f"Email:     {prospect['contact_email']}")
    if prospect.get("phone"):
        lines.append(f"Phone:     {prospect['phone']}")
    if prospect.get("website"):
        lines.append(f"Website:   {prospect['website']}")
    lines += [
        f"Niche:     {prospect.get('niche')}",
        "",
        "— ORIGINAL REPLY —",
        f"Subject: {reply.get('subject') or '(none)'}",
        f"Received: {reply.get('received_at')}",
        "",
        reply.get("body") or "(empty)",
        "",
        (cfg.get("include_signature") or "— LeadGen AI")
    ]
    return "\n".join(lines)


def forward(reply_id: str, *, operator_name: str | None = None) -> dict[str, Any]:
    """Forward one already-persisted reply to its agency client."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT r.*, p.business_name AS pname, p.contact_name AS pcontact, "
        "       p.contact_email AS pemail, p.contact_title AS ptitle, "
        "       p.phone AS pphone, p.website AS pweb, p.city AS pcity, p.niche AS pniche "
        "FROM replies r LEFT JOIN prospects p ON r.prospect_id=p.id "
        "WHERE r.id=?",
        (reply_id,),
    ).fetchone()
    if row is None:
        return {"ok": False, "error": "reply_not_found"}
    if not row["client_id"]:
        return {"ok": False, "error": "no_client_attached"}
    if row["forwarded"]:
        return {"ok": False, "error": "already_forwarded"}
    if row["intent"] not in ("HOT", "MORE_INFO"):
        return {"ok": False, "error": f"intent_not_hot:{row['intent']}"}

    client = conn.execute("SELECT * FROM clients WHERE id=?", (row["client_id"],)).fetchone()
    if not client or not client["contact_email"]:
        return {"ok": False, "error": "no_client_email"}

    settings_agent = config.load_settings().get("agent", {})
    operator_from = settings_agent.get("smtp_user") or settings_agent.get("from_address")
    if not operator_from:
        return {"ok": False, "error": "operator_email_unset"}

    subject = f"Hot lead for {client['name']}: {row['pname']}"
    body = _format_body(
        dict(row),
        {
            "business_name": row["pname"],
            "contact_name": row["pcontact"],
            "contact_email": row["pemail"],
            "contact_title": row["ptitle"],
            "phone": row["pphone"],
            "website": row["pweb"],
            "city": row["pcity"],
            "niche": row["pniche"],
        },
        dict(client),
    )
    msg = _build_forward_email(
        operator_from=operator_from,
        client_email=client["contact_email"],
        subject=subject,
        body=body,
        operator_name=operator_name,
    )

    if not (settings_agent.get("smtp_host") and settings_agent.get("smtp_user") and settings_agent.get("smtp_pass")):
        return {"ok": False, "error": "smtp_credentials_missing"}

    try:
        host = settings_agent["smtp_host"]
        port = int(settings_agent.get("smtp_port") or 587)
        tls_mode = (settings_agent.get("smtp_tls") or "starttls").lower()
        if port == 465 or tls_mode == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(settings_agent["smtp_user"], settings_agent["smtp_pass"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                if tls_mode in ("starttls", "tls"):
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                s.login(settings_agent["smtp_user"], settings_agent["smtp_pass"])
                s.send_message(msg)
        conn.execute(
            "UPDATE replies SET forwarded=1, forwarded_at=? WHERE id=?",
            (db._now(), reply_id),
        )
        db.audit("forward.ok", {"reply_id": reply_id, "client_id": row["client_id"]})
        return {"ok": True, "client_email": client["contact_email"], "subject": subject}
    except Exception as exc:
        LOG.exception("forward failed")
        db.audit("forward.failed", {"reply_id": reply_id, "err": str(exc)})
        return {"ok": False, "error": str(exc)}
