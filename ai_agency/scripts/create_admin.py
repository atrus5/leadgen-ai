#!/usr/bin/env python3
"""
One-shot bootstrap: create the first (or an additional) platform-admin
account. There's no self-serve admin signup route by design — this script
is the only way to create one, alongside scripts/migrate_to_workspace.py
(which seeds an admin from an existing single-tenant install's password).

Usage::

    python -m ai_agency.scripts.create_admin --email paul@example.com
    # prompts for a password (not echoed) unless --password is given
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO))

import bcrypt  # noqa: E402

from ai_agency import platform_db  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="if omitted, prompted interactively (not echoed)")
    args = p.parse_args()

    platform_db.init_schema()
    platform_db.ensure_default_platform_settings()

    email = args.email.strip().lower()
    if platform_db.get_admin_by_email(email):
        print(f"An admin account already exists for {email}.")
        return 1

    pw = args.password or getpass.getpass("New admin password (min 8 chars): ")
    if len(pw) < 8:
        print("Password must be at least 8 characters.")
        return 2

    ph = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    admin = platform_db.create_admin(email=email, password_hash=ph)
    print(f"Created platform admin {admin['email']} (id={admin['id']}).")
    print("Log in at /admin/login.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
