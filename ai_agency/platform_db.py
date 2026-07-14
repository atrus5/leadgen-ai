"""
Platform-level database: workspaces (tenants), platform admins, per-workspace
users, and pending invites. This is a true process-wide singleton — unlike
`db.py` (one SQLite file *per workspace*), there is exactly one platform.db
for the whole install, so it reuses the original single-connection pattern
`db.py` had before it went workspace-aware.

Tables:
  workspaces        - one row per paying customer / tenant
  platform_admins    - Paul (and anyone else running the platform itself)
  users              - one row per login; every user belongs to exactly one
                       workspace (role: 'owner' | 'member')
  invites            - pending/accepted/revoked invite tokens, reused for
                       both "admin invites a workspace's first owner" and
                       "workspace owner invites a teammate"
  platform_settings  - key/value store, currently just the platform's own
                       outbound mailbox used to send invite emails
  platform_audit     - audit trail for admin actions
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "platform.db"

_LOCAL = threading.local()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',   -- active | suspended | cancelled
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by_admin_id TEXT REFERENCES platform_admins(id) ON DELETE SET NULL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS workspaces_status_idx ON workspaces(status);

CREATE TABLE IF NOT EXISTS platform_admins (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',      -- 'owner' | 'member'
    status TEXT NOT NULL DEFAULT 'active',    -- 'active' | 'disabled'
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS users_workspace_idx ON users(workspace_id);

CREATE TABLE IF NOT EXISTS invites (
    token TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    invited_by_type TEXT NOT NULL,            -- 'platform_admin' | 'user'
    invited_by_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted | revoked | expired
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_user_id TEXT REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS invites_workspace_idx ON invites(workspace_id);
CREATE INDEX IF NOT EXISTS invites_email_idx ON invites(email);

CREATE TABLE IF NOT EXISTS platform_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_db() -> sqlite3.Connection:
    """Thread-local cached connection to the single platform.db."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = _connect()
        _LOCAL.conn = conn
    return conn


def init_schema() -> None:
    """Create tables. Safe to call repeatedly. Platform.db is a true
    singleton, so — unlike per-workspace agency.db — this can run eagerly
    at process boot."""
    get_db().executescript(SCHEMA)


# ── Settings helpers (same shape as db.py's, for the invite mailbox) ──────

SENSITIVE_KEYS = ("invite_mailbox",)
_SENSITIVE_FIELD_PATHS = {"invite_mailbox": ("smtp_pass",)}


def get_setting(key: str, default: Any = None) -> Any:
    row = get_db().execute("SELECT value FROM platform_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        value = json.loads(row["value"])
    except json.JSONDecodeError:
        return default
    if key in SENSITIVE_KEYS:
        try:
            from . import secret_keeper as _sk
            value = _sk.unwrap(value)
        except Exception:
            pass
    return value


def set_setting(key: str, value: Any) -> None:
    if key in SENSITIVE_KEYS:
        try:
            from . import secret_keeper as _sk
            value = _sk.wrap(value, field_paths=_SENSITIVE_FIELD_PATHS.get(key, ()))
        except Exception:
            pass
    get_db().execute(
        "INSERT INTO platform_settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), _now()),
    )


DEFAULT_PLATFORM_SETTINGS = {
    # The mailbox Paul controls, used to send every invite email regardless
    # of whether it's "admin invites workspace owner" or "owner invites
    # teammate" — a brand-new workspace has no SMTP of its own configured
    # yet, so invites can never go out through a workspace's own settings.
    "invite_mailbox": {
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_pass": "",
        "smtp_tls": "starttls",
        "from_name": "LeadGen AI",
        "from_address": "",
        "base_url": "",
    },
}


def ensure_default_platform_settings() -> None:
    db = get_db()
    existing = {row["key"] for row in db.execute("SELECT key FROM platform_settings").fetchall()}
    for key, value in DEFAULT_PLATFORM_SETTINGS.items():
        if key not in existing:
            set_setting(key, value)


# ── Audit ───────────────────────────────────────────────────────────────

def audit(kind: str, detail: Any = None) -> None:
    try:
        get_db().execute(
            "INSERT INTO platform_audit(kind, detail, created_at) VALUES(?,?,?)",
            (kind, json.dumps(detail) if detail is not None else None, _now()),
        )
    except sqlite3.OperationalError:
        pass


# ── Workspace helpers ───────────────────────────────────────────────────

def create_workspace(*, name: str, created_by_admin_id: str | None, notes: str | None = None) -> dict[str, Any]:
    import uuid
    wid = uuid.uuid4().hex
    slug = _slugify(name)
    get_db().execute(
        "INSERT INTO workspaces(id, name, slug, status, created_at, updated_at, created_by_admin_id, notes) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (wid, name, slug, "active", _now(), _now(), created_by_admin_id, notes),
    )
    audit("workspace.created", {"workspace_id": wid, "name": name})
    return dict(get_workspace(wid))


def _slugify(name: str) -> str:
    import re
    import uuid
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "workspace"
    slug = base
    n = 1
    while get_db().execute("SELECT 1 FROM workspaces WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
        if n > 50:
            return f"{base}-{uuid.uuid4().hex[:6]}"
    return slug


def get_workspace(workspace_id: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()


def list_workspaces() -> list[sqlite3.Row]:
    return get_db().execute("SELECT * FROM workspaces ORDER BY created_at DESC").fetchall()


def list_active_workspaces() -> list[sqlite3.Row]:
    return get_db().execute("SELECT * FROM workspaces WHERE status='active' ORDER BY created_at").fetchall()


def set_workspace_status(workspace_id: str, status: str) -> None:
    get_db().execute(
        "UPDATE workspaces SET status=?, updated_at=? WHERE id=?",
        (status, _now(), workspace_id),
    )
    audit("workspace.status_changed", {"workspace_id": workspace_id, "status": status})


# ── User helpers ────────────────────────────────────────────────────────

def get_user_by_email(email: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()


def get_user(user_id: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def list_users_for_workspace(workspace_id: str) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM users WHERE workspace_id=? ORDER BY created_at", (workspace_id,)
    ).fetchall()


def create_user(*, workspace_id: str, email: str, password_hash: str, role: str, name: str | None = None) -> dict[str, Any]:
    import uuid
    uid = uuid.uuid4().hex
    get_db().execute(
        "INSERT INTO users(id, workspace_id, email, name, password_hash, role, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, workspace_id, email.strip().lower(), name, password_hash, role, "active", _now(), _now()),
    )
    audit("user.created", {"user_id": uid, "workspace_id": workspace_id, "role": role})
    return dict(get_user(uid))


def touch_user_login(user_id: str) -> None:
    get_db().execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), user_id))


def set_user_password(user_id: str, password_hash: str) -> None:
    get_db().execute(
        "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
        (password_hash, _now(), user_id),
    )


def set_user_status(user_id: str, status: str) -> None:
    get_db().execute(
        "UPDATE users SET status=?, updated_at=? WHERE id=?",
        (status, _now(), user_id),
    )
    audit("user.status_changed", {"user_id": user_id, "status": status})


# ── Admin helpers ───────────────────────────────────────────────────────

def get_admin_by_email(email: str) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM platform_admins WHERE email=?", (email.strip().lower(),)
    ).fetchone()


def get_admin(admin_id: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM platform_admins WHERE id=?", (admin_id,)).fetchone()


def create_admin(*, email: str, password_hash: str) -> dict[str, Any]:
    import uuid
    aid = uuid.uuid4().hex
    get_db().execute(
        "INSERT INTO platform_admins(id, email, password_hash, created_at) VALUES(?,?,?,?)",
        (aid, email.strip().lower(), password_hash, _now()),
    )
    audit("admin.created", {"admin_id": aid, "email": email})
    return dict(get_admin(aid))


def touch_admin_login(admin_id: str) -> None:
    get_db().execute("UPDATE platform_admins SET last_login_at=? WHERE id=?", (_now(), admin_id))


# ── Invite helpers ──────────────────────────────────────────────────────

def create_invite(*, workspace_id: str, email: str, role: str, invited_by_type: str, invited_by_id: str, ttl_hours: int = 72) -> dict[str, Any]:
    import secrets as _secrets
    from datetime import timedelta
    token = _secrets.token_urlsafe(24)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    get_db().execute(
        "INSERT INTO invites(token, workspace_id, email, role, invited_by_type, invited_by_id, "
        "status, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (token, workspace_id, email.strip().lower(), role, invited_by_type, invited_by_id,
         "pending", _now(), expires_at),
    )
    audit("invite.created", {"workspace_id": workspace_id, "email": email, "role": role})
    return dict(get_invite(token))


def get_invite(token: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()


def list_invites_for_workspace(workspace_id: str) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM invites WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,)
    ).fetchall()


def mark_invite_accepted(token: str, user_id: str) -> None:
    get_db().execute(
        "UPDATE invites SET status='accepted', accepted_at=?, accepted_user_id=? WHERE token=?",
        (_now(), user_id, token),
    )


def revoke_invite(token: str) -> None:
    get_db().execute("UPDATE invites SET status='revoked' WHERE token=?", (token,))
    audit("invite.revoked", {"token": token})


def invite_is_valid(invite: sqlite3.Row) -> bool:
    if invite["status"] != "pending":
        return False
    expires_at = datetime.fromisoformat(invite["expires_at"].replace("Z", "+00:00"))
    return expires_at > datetime.now(timezone.utc)
