#!/usr/bin/env python3
"""
One-shot migration: convert a pre-multi-tenant single-DB install
(data/agency.db) into workspace #1 under the new per-workspace layout
(data/workspaces/<id>/agency.db), and bootstrap Paul's own login as both
a platform admin (so he can manage other tenants) and that workspace's
owner (so he can log straight into his existing data).

No-ops safely if there's nothing to migrate, or if this has already run
once (tagged via workspaces.notes='migrated_from_single_tenant').

Usage::

    python -m ai_agency.scripts.migrate_to_workspace \\
        --workspace-name "My Agency" --admin-email paul@example.com

    # Or fully interactive (prompts for both):
    python -m ai_agency.scripts.migrate_to_workspace
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO))

from ai_agency import db, platform_db  # noqa: E402


def _read_legacy_settings(legacy_path: Path) -> dict[str, dict]:
    """Read the settings table directly out of the legacy single-file DB
    without going through db.py's workspace-aware get_db() (there's no
    workspace yet at this point — this file itself IS about to become one)."""
    conn = sqlite3.connect(legacy_path)
    conn.row_factory = sqlite3.Row
    out: dict[str, dict] = {}
    for row in conn.execute("SELECT key, value FROM settings").fetchall():
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            continue
    conn.close()
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workspace-name", help="name for the migrated workspace; prompted if omitted")
    p.add_argument("--admin-email", help="Paul's admin + owner login email; prompted if omitted")
    p.add_argument("--dry-run", action="store_true", help="report what would happen; don't write")
    args = p.parse_args()

    legacy_path = db.LEGACY_DB_PATH
    if not legacy_path.exists():
        print(f"No legacy database at {legacy_path} — nothing to migrate.")
        return 0

    platform_db.init_schema()
    platform_db.ensure_default_platform_settings()

    # Idempotency guard: refuse to run twice.
    already = [
        w for w in platform_db.list_workspaces()
        if (w["notes"] or "") == "migrated_from_single_tenant"
    ]
    if already:
        print(f"Already migrated — workspace {already[0]['id']} ({already[0]['name']}) "
              f"is tagged migrated_from_single_tenant. Refusing to run again.")
        return 1

    legacy_settings = _read_legacy_settings(legacy_path)
    auth_setting = legacy_settings.get("auth") or {}
    password_hash = auth_setting.get("password_hash")
    if isinstance(password_hash, dict):
        # Encrypted at rest (secret_keeper wraps it as {"__enc__": "..."}).
        # Unwrap using the SAME LEADGEN_MASTER_KEY the original install used.
        from ai_agency import secret_keeper
        password_hash = secret_keeper.unwrap(password_hash)
    if not password_hash:
        print("No existing dashboard password found in the legacy DB "
              "(settings.auth.password_hash is empty) — nothing to carry over "
              "for login. Aborting; set one up via a fresh invite instead.")
        return 2

    ws_name = args.workspace_name or input("Name for this workspace [My Agency]: ").strip() or "My Agency"
    admin_email = (args.admin_email or input("Your admin/owner email: ").strip()).lower()
    if not admin_email or "@" not in admin_email:
        print("A valid email is required.")
        return 2

    if args.dry_run:
        print(f"DRY RUN — would create workspace {ws_name!r}, move {legacy_path} into it, "
              f"and seed platform_admins + users for {admin_email}.")
        return 0

    # Fold the WAL into the main file before moving it, so nothing is left
    # behind in orphaned -wal/-shm siblings.
    try:
        wal_conn = sqlite3.connect(legacy_path)
        wal_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wal_conn.close()
    except sqlite3.Error as exc:
        print(f"Warning: WAL checkpoint failed ({exc}); continuing anyway.")

    ws = platform_db.create_workspace(name=ws_name, created_by_admin_id=None, notes="migrated_from_single_tenant")
    dest_dir = db.WORKSPACES_DIR / ws["id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / "agency.db"
    shutil.move(str(legacy_path), str(dest_path))
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(legacy_path) + suffix)
        if sidecar.exists():
            shutil.move(str(sidecar), str(dest_path) + suffix)
    print(f"Moved {legacy_path} -> {dest_path}")

    # Seed the admin account (for managing other workspaces later).
    admin = platform_db.get_admin_by_email(admin_email)
    if not admin:
        admin = platform_db.create_admin(email=admin_email, password_hash=password_hash)
        print(f"Created platform admin {admin_email}.")
    else:
        print(f"Platform admin {admin_email} already existed — left as-is.")

    # Seed the owner account for the migrated workspace, reusing the SAME
    # bcrypt hash (portable) so the existing password keeps working.
    owner = platform_db.get_user_by_email(admin_email)
    if not owner:
        owner = platform_db.create_user(
            workspace_id=ws["id"], email=admin_email, password_hash=password_hash, role="owner",
        )
        print(f"Created workspace-owner login {admin_email} for {ws_name}.")
    else:
        print(f"A user already exists for {admin_email} — left as-is (workspace_id={owner['workspace_id']}).")

    # Clean up now-dead single-tenant auth keys inside the migrated DB;
    # webhook_secret stays (still used, now scoped to this workspace).
    db.use_workspace(ws["id"])
    conn = db.get_db()
    conn.execute("DELETE FROM settings WHERE key IN ('auth','auth_pending_setup_token')")
    print("Removed legacy settings.auth / settings.auth_pending_setup_token from the migrated workspace.")

    print()
    print(f"Done. Workspace: {ws['name']} ({ws['id']})")
    print(f"Log in at / with {admin_email} and your existing dashboard password.")
    print(f"Manage other workspaces at /admin with the same email/password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
