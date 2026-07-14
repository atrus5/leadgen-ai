"""
Per-workspace, multi-user auth gate for the agency dashboard + webhook
protection.

Design notes
============
- Every login belongs to exactly one workspace (tenant). There is no more
  single global password — accounts live in platform_db.users, looked up by
  globally-unique email. Session carries user_id + workspace_id + role
  ('owner' | 'member'), not just a bare authed flag.
- Accounts are created exclusively via invite-accept (see invites.py) —
  there is no self-serve signup and no one-time install-token bootstrap
  anymore (that concept doesn't fit multi-tenant: which workspace would an
  anonymous installer even be installing into?).
- Flask session cookie (HttpOnly, SameSite=Lax). Secure flag is toggled on
  automatically when tracking.base_url starts with https:// (see app.py).
- CSRF is mitigated by SameSite=Lax AND the requirement that mutating
  endpoints (login, accept-invite, change-password) accept only
  Content-Type: application/json.
- bcrypt cost=12 for password hashing, same as before.
- `/run-audit` and `/new-lead` are NOT part of `/api/*` (see app.py) and are
  instead gated by `?token=<webhook_secret>`, generated per-workspace on
  first touch by `ensure_webhook_secret()`.
- Platform-admin auth (Paul's super-admin console) is a SEPARATE, parallel
  system — see admin.py. It never touches db.use_workspace() and uses its
  own session key (session["admin_id"]) so one email existing as both an
  admin and a workspace user is never ambiguous.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

import bcrypt
from flask import Blueprint, jsonify, request, session

from . import db, platform_db

LOG = logging.getLogger("ai_agency.auth")

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# ── Pure JSON helpers (don't import app._ok to avoid a circular import) ────

def _ok(payload: Any = None, **extra: Any):
    out: dict[str, Any] = {"ok": True}
    if payload is not None:
        out["data"] = payload
    out.update(extra)
    return jsonify(out)


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


# ── Session helpers ─────────────────────────────────────────────────────

def is_authed() -> bool:
    return bool(session.get("user_id") and session.get("workspace_id"))


def current_user_id() -> str | None:
    return session.get("user_id")


def current_role() -> str | None:
    return session.get("role")


def _login_session(user: dict[str, Any]) -> None:
    session.clear()
    session["user_id"] = user["id"]
    session["workspace_id"] = user["workspace_id"]
    session["role"] = user["role"]
    session["login_at"] = db._now()


def _webhook_secret() -> str:
    s = db.get_setting("webhook_secret")
    return (s or {}).get("value") if isinstance(s, dict) else (s or "")


def ensure_webhook_secret() -> str:
    """Make sure the CURRENT workspace's webhook secret exists. Returns it.
    Generates on first touch only; never rotates silently. Called from
    app.py's before_request right after db.use_workspace(), so it's always
    ready before any route body runs — no more boot-time global call."""
    existing = _webhook_secret()
    if existing:
        return existing
    secret = secrets.token_urlsafe(24)  # 32 chars
    db.set_setting("webhook_secret", {"value": secret, "issued_at": db._now()})
    db.audit("webhook_secret.generated", {})
    return secret


# ── Endpoints ────────────────────────────────────────────────────────────

@auth_bp.route("/status", methods=["GET"])
def status():
    if not is_authed():
        return _ok({"authed": False})
    return _ok({
        "authed": True,
        "user_id": session.get("user_id"),
        "workspace_id": session.get("workspace_id"),
        "role": session.get("role"),
    })


@auth_bp.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    if not email or not pw:
        return _err("email_and_password_required", 422)
    user = platform_db.get_user_by_email(email)
    if not user:
        # Pay the bcrypt cost anyway so a nonexistent-email probe takes the
        # same time as a wrong-password one (timing side-channel hygiene).
        bcrypt.checkpw(pw.encode("utf-8"), bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=12)))
        return _err("invalid_credentials", 401)
    try:
        ok = bcrypt.checkpw(pw.encode("utf-8"), user["password_hash"].encode("ascii"))
    except (ValueError, TypeError):
        ok = False
    if not ok:
        return _err("invalid_credentials", 401)
    if user["status"] != "active":
        return _err("account_disabled", 403)
    ws = platform_db.get_workspace(user["workspace_id"])
    if not ws or ws["status"] != "active":
        return _err("workspace_inactive", 403)
    _login_session(dict(user))
    platform_db.touch_user_login(user["id"])
    platform_db.audit("user.login_ok", {"user_id": user["id"], "workspace_id": user["workspace_id"]})
    return _ok({"authed": True, "workspace_id": user["workspace_id"], "role": user["role"]})


@auth_bp.route("/accept-invite", methods=["POST"])
def accept_invite():
    """Redeem an invite token (from either "admin invites workspace owner"
    or "owner invites teammate" — same mechanism, see invites.py) by
    setting a password and creating the users row. Auto-logs-in on success,
    same pattern the old single-tenant install() used."""
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    pw = body.get("password") or ""
    pw2 = body.get("password_confirm") or ""
    name = (body.get("name") or "").strip() or None
    if not token:
        return _err("token_required", 422)
    if len(pw) < 8:
        return _err("password_too_short_min_8", 422)
    if pw != pw2:
        return _err("password_confirm_mismatch", 422)

    invite = platform_db.get_invite(token)
    if not invite or not platform_db.invite_is_valid(invite):
        return _err("invalid_or_expired_invite", 422)
    if platform_db.get_user_by_email(invite["email"]):
        return _err("account_already_exists_for_this_email", 409)
    ws = platform_db.get_workspace(invite["workspace_id"])
    if not ws or ws["status"] != "active":
        return _err("workspace_inactive", 403)

    ph = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    user = platform_db.create_user(
        workspace_id=invite["workspace_id"], email=invite["email"],
        password_hash=ph, role=invite["role"], name=name,
    )
    platform_db.mark_invite_accepted(token, user["id"])
    platform_db.audit("invite.accepted", {"token": token, "user_id": user["id"]})
    _login_session(user)
    return _ok({"authed": True, "workspace_id": user["workspace_id"], "role": user["role"]})


@auth_bp.route("/logout", methods=["POST"])
def logout():
    was = is_authed()
    session.clear()
    if was:
        platform_db.audit("user.logout", {})
    return _ok({"authed": False})


@auth_bp.route("/change-password", methods=["POST"])
def change_password():
    """Change the current user's password (requires current password)."""
    if not is_authed():
        return _err("auth_required", 401)
    body = request.get_json(silent=True) or {}
    cur = body.get("current_password") or ""
    new = body.get("new_password") or ""
    new2 = body.get("new_password_confirm") or ""
    if len(new) < 8:
        return _err("password_too_short_min_8", 422)
    if new != new2:
        return _err("password_confirm_mismatch", 422)
    user = platform_db.get_user(session["user_id"])
    if not user:
        return _err("server_state_error_no_user", 500)
    try:
        ok = bcrypt.checkpw(cur.encode("utf-8"), user["password_hash"].encode("ascii"))
    except (ValueError, TypeError):
        ok = False
    if not ok:
        return _err("current_password_invalid", 401)
    ph = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    platform_db.set_user_password(user["id"], ph)
    platform_db.audit("user.password_rotated", {"user_id": user["id"]})
    return _ok({"rotated": True})


# ── Webhook URL helper (logged-in workspace user only) ──────────────────

@auth_bp.route("/webhook/url", methods=["GET"])
def webhook_url():
    if not is_authed():
        return _err("auth_required", 401)
    from .config import tracking_base_url
    base = tracking_base_url().rstrip("/")
    if not base:
        return _err(
            "tracking.base_url unset — set it in /wizard before the "
            "webhook URLs are useful.",
            422,
        )
    sec = ensure_webhook_secret()
    ws_id = session["workspace_id"]
    # Show first 4 + last 4 of the secret for visual confirmation; don't
    # leak the whole thing in case it ends up in a screenshot.
    masked = (sec[:4] + "…" + sec[-4:]) if len(sec) >= 9 else "set"
    return _ok({
        "run_audit_url": f"{base}/w/{ws_id}/run-audit?token={sec}",
        "new_lead_url": f"{base}/w/{ws_id}/new-lead?token={sec}",
        "secret_masked": masked,
    })
