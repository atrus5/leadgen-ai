"""
Invite creation + email delivery. Both "admin invites a workspace's first
owner" (see admin.py) and "workspace owner invites a teammate" (see
app.py's POST /api/team/invite) go through the same two functions here —
the only difference is who's calling and what role/workspace they pass in.

Every invite email is sent through the PLATFORM mailbox
(platform_settings["invite_mailbox"]), never a workspace's own SMTP, since
a brand-new workspace has no SMTP configured yet — that's the whole reason
this lives at the platform level instead of in outreach/sender.py.

Acceptance (POST /api/auth/accept-invite) lives in auth.py, not here — it's
an HTTP-request-shaped operation (token/password/password_confirm/name from
the request body) with exactly one caller, so there's nothing to share by
extracting it.
"""
from __future__ import annotations

import logging
from typing import Any

from . import mail, platform_db

LOG = logging.getLogger("ai_agency.invites")


def create_invite(*, workspace_id: str, email: str, role: str, invited_by_type: str, invited_by_id: str) -> dict[str, Any]:
    return platform_db.create_invite(
        workspace_id=workspace_id, email=email, role=role,
        invited_by_type=invited_by_type, invited_by_id=invited_by_id,
    )


def send_invite_email(invite: dict[str, Any], workspace_name: str) -> dict[str, Any]:
    """Send the invite link via the platform mailbox. Returns
    {"ok": True} or {"ok": False, "error": str, "link": str} — the link is
    included even on failure so an admin/owner can hand it over manually if
    the platform mailbox isn't configured yet."""
    mailbox = platform_db.get_setting("invite_mailbox") or {}
    base = (mailbox.get("base_url") or "").rstrip("/")
    if not base:
        LOG.warning("invite_mailbox.base_url not configured — invite %s created but no link could be built", invite["token"])
        return {"ok": False, "error": "platform invite_mailbox.base_url not configured"}

    link = f"{base}/accept-invite.html?token={invite['token']}"
    role_label = "an owner" if invite["role"] == "owner" else "a team member"
    subject = f"You're invited to {workspace_name} on LeadGen AI"
    body = (
        f"Hi,\n\n"
        f"You've been invited to join \"{workspace_name}\" on LeadGen AI as {role_label}.\n\n"
        f"Set your password here (link expires in 72 hours):\n{link}\n\n"
        f"If you weren't expecting this, you can safely ignore this email.\n"
    )

    if not (mailbox.get("smtp_host") and mailbox.get("smtp_user") and mailbox.get("smtp_pass")):
        LOG.warning("invite_mailbox SMTP not fully configured — invite created but email NOT sent. Link: %s", link)
        return {"ok": False, "error": "invite_mailbox smtp not fully configured", "link": link}

    from_name = mailbox.get("from_name") or "LeadGen AI"
    from_addr = mailbox.get("from_address") or mailbox.get("smtp_user")
    result = mail.send_plain_email(mailbox, f"{from_name} <{from_addr}>", invite["email"], subject, body)
    result["link"] = link
    return result
