"""
Platform-admin console API — Paul's super-admin surface for creating and
managing workspaces (paying customers). Entirely separate auth system from
auth.py's per-workspace user login: separate table (platform_admins),
separate session key (session["admin_id"]), never touches
db.use_workspace(). This keeps "is this email a workspace user or a
platform admin" always unambiguous — an admin never accidentally gets
tenant-scoped session state, and vice versa.

Bootstrap: the first admin account is created by
scripts/create_admin.py (fresh install) or by
scripts/migrate_to_workspace.py (existing single-tenant install being
converted) — there is no self-serve admin signup route, deliberately.
"""
from __future__ import annotations

import logging
from typing import Any

import bcrypt
from flask import Blueprint, jsonify, request, session

from . import invites, platform_db

LOG = logging.getLogger("ai_agency.admin")

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

ADMIN_AUTH_EXEMPT = {"admin.login", "admin.status"}


def _ok(payload: Any = None, **extra: Any):
    out: dict[str, Any] = {"ok": True}
    if payload is not None:
        out["data"] = payload
    out.update(extra)
    return jsonify(out)


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def is_admin_authed() -> bool:
    return bool(session.get("admin_id"))


@admin_bp.before_request
def admin_before_request():
    endpoint = request.endpoint or ""
    if endpoint in ADMIN_AUTH_EXEMPT:
        return None
    if not is_admin_authed():
        return _err("admin_auth_required", 401)
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        ct = (request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ct and ct != "application/json":
            return _err("content_type_must_be_application_json", 415)
    return None


# ── Admin auth ──────────────────────────────────────────────────────────

@admin_bp.route("/status", methods=["GET"])
def status():
    return _ok({"authed": is_admin_authed(), "admin_id": session.get("admin_id")})


@admin_bp.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    if not email or not pw:
        return _err("email_and_password_required", 422)
    admin = platform_db.get_admin_by_email(email)
    if not admin:
        bcrypt.checkpw(pw.encode("utf-8"), bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=12)))
        return _err("invalid_credentials", 401)
    try:
        ok = bcrypt.checkpw(pw.encode("utf-8"), admin["password_hash"].encode("ascii"))
    except (ValueError, TypeError):
        ok = False
    if not ok:
        return _err("invalid_credentials", 401)
    session.clear()
    session["admin_id"] = admin["id"]
    platform_db.touch_admin_login(admin["id"])
    platform_db.audit("admin.login_ok", {"admin_id": admin["id"]})
    return _ok({"authed": True})


@admin_bp.route("/logout", methods=["POST"])
def logout():
    was = is_admin_authed()
    session.clear()
    if was:
        platform_db.audit("admin.logout", {})
    return _ok({"authed": False})


# ── Workspace management ───────────────────────────────────────────────

@admin_bp.route("/workspaces", methods=["GET"])
def list_workspaces():
    out = []
    for ws in platform_db.list_workspaces():
        d = dict(ws)
        d["user_count"] = len(platform_db.list_users_for_workspace(ws["id"]))
        out.append(d)
    return _ok(out)


@admin_bp.route("/workspaces", methods=["POST"])
def create_workspace():
    """Create a workspace and invite its first user (the owner)."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    owner_email = (body.get("owner_email") or "").strip().lower()
    if not name or not owner_email:
        return _err("name_and_owner_email_required", 422)
    if platform_db.get_user_by_email(owner_email):
        return _err("an_account_already_exists_for_that_email", 409)

    ws = platform_db.create_workspace(name=name, created_by_admin_id=session["admin_id"])
    invite = invites.create_invite(
        workspace_id=ws["id"], email=owner_email, role="owner",
        invited_by_type="platform_admin", invited_by_id=session["admin_id"],
    )
    send_result = invites.send_invite_email(invite, ws["name"])
    return _ok({
        "workspace": ws,
        "invite": {"token": invite["token"], "email": invite["email"], "expires_at": invite["expires_at"]},
        "email_sent": send_result.get("ok", False),
        "invite_link": send_result.get("link"),
        "email_error": send_result.get("error"),
    })


@admin_bp.route("/workspaces/<workspace_id>", methods=["GET"])
def get_workspace(workspace_id: str):
    ws = platform_db.get_workspace(workspace_id)
    if not ws:
        return _err("not_found", 404)
    d = dict(ws)
    d["users"] = [dict(u) for u in platform_db.list_users_for_workspace(workspace_id)]
    d["invites"] = [dict(i) for i in platform_db.list_invites_for_workspace(workspace_id)]
    return _ok(d)


@admin_bp.route("/workspaces/<workspace_id>/suspend", methods=["POST"])
def suspend_workspace(workspace_id: str):
    if not platform_db.get_workspace(workspace_id):
        return _err("not_found", 404)
    platform_db.set_workspace_status(workspace_id, "suspended")
    return _ok({"id": workspace_id, "status": "suspended"})


@admin_bp.route("/workspaces/<workspace_id>/resume", methods=["POST"])
def resume_workspace(workspace_id: str):
    if not platform_db.get_workspace(workspace_id):
        return _err("not_found", 404)
    platform_db.set_workspace_status(workspace_id, "active")
    return _ok({"id": workspace_id, "status": "active"})


@admin_bp.route("/workspaces/<workspace_id>/invites", methods=["POST"])
def reinvite(workspace_id: str):
    """Send another invite for a workspace (e.g. the owner invite expired,
    or hiring a second early user before the first has accepted)."""
    ws = platform_db.get_workspace(workspace_id)
    if not ws:
        return _err("not_found", 404)
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    role = body.get("role") or "member"
    if not email:
        return _err("email_required", 422)
    if role not in ("owner", "member"):
        return _err("invalid_role", 422)
    if platform_db.get_user_by_email(email):
        return _err("an_account_already_exists_for_that_email", 409)
    invite = invites.create_invite(
        workspace_id=workspace_id, email=email, role=role,
        invited_by_type="platform_admin", invited_by_id=session["admin_id"],
    )
    send_result = invites.send_invite_email(invite, ws["name"])
    return _ok({
        "invite": {"token": invite["token"], "email": invite["email"], "expires_at": invite["expires_at"]},
        "email_sent": send_result.get("ok", False),
        "invite_link": send_result.get("link"),
        "email_error": send_result.get("error"),
    })


# ── Platform settings (invite mailbox) ───────────────────────────────────

@admin_bp.route("/settings/invite-mailbox", methods=["GET"])
def get_invite_mailbox():
    mb = platform_db.get_setting("invite_mailbox") or {}
    mb = dict(mb)
    mb.pop("smtp_pass", None)  # never echo the secret back
    return _ok(mb)


@admin_bp.route("/settings/invite-mailbox", methods=["PUT"])
def put_invite_mailbox():
    body = request.get_json(silent=True) or {}
    cur = platform_db.get_setting("invite_mailbox") or {}
    cur.update({k: v for k, v in body.items() if k in (
        "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_tls",
        "from_name", "from_address", "base_url",
    )})
    platform_db.set_setting("invite_mailbox", cur)
    return _ok({"saved": True})
