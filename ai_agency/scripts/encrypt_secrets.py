#!/usr/bin/env python3
"""
One-shot migration tool: walk the `settings` table and re-wrap any
plaintext sensitive keys into ciphertext.

Why a separate CLI?
===================
The orchestrator's runtime path auto-encrypts on write and auto-decrypts
on read \u2014 so once you set LEADGEN_MASTER_KEY, every NEW write is safe.
But secrets that were stored BEFORE you set the env var are still
plaintext. Run this once after enabling LEADGEN_MASTER_KEY to convert
them in-place.

Usage (one workspace, or sweep all of them — each has its own SQLite
file now, so there's no single "the" settings table anymore)::

    export LEADGEN_MASTER_KEY=<your-strong-passphrase>
    python -m ai_agency.scripts.encrypt_secrets --workspace <id>
    python -m ai_agency.scripts.encrypt_secrets --all-workspaces
    python -m ai_agency.scripts.encrypt_secrets --all-workspaces --dry-run

The script is idempotent on the encrypted flag \u2014 hitting a row that is
already wrapped is a no-op. Run it multiple times if needed.

Side effect beyond the row updates: writes a `settings.migrated_to_encryption`
audit entry so the dashboard can show a clean "all secrets encrypted" badge.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make `ai_agency` resolvable when run from repo root or from the
# scripts/ subdir alike.
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO))

from ai_agency import db, platform_db, secret_keeper  # noqa: E402

SENSITIVE_KEYS = list(db.SENSITIVE_KEYS)


def _run_for_workspace(workspace_id: str, *, dry_run: bool, sk) -> tuple[int, int]:
    db.use_workspace(workspace_id)
    conn = db.get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    changed = 0
    skipped = 0
    for r in rows:
        if r["key"] not in SENSITIVE_KEYS:
            continue
        try:
            cur = json.loads(r["value"])
        except json.JSONDecodeError:
            continue
        if isinstance(cur, dict):
            already_enc = any(v.startswith("enc:") for v in cur.values() if isinstance(v, str))
        elif isinstance(cur, str):
            already_enc = cur.startswith("enc:")
        else:
            already_enc = False
        if already_enc:
            skipped += 1
            continue
        new = sk.wrap(cur, field_paths=db._SENSITIVE_FIELD_PATHS.get(r["key"], ()))
        if dry_run:
            print(f"  WOULD wrap settings.{r['key']}")
        else:
            conn.execute(
                "UPDATE settings SET value=?, updated_at=? WHERE key=?",
                (json.dumps(new), db._now(), r["key"]),
            )
            print(f"  wrapped settings.{r['key']}")
        changed += 1

    if not dry_run and changed:
        db.audit("settings.migrated_to_encryption", {"changed": changed, "by": "encrypt_secrets.py"})
    return changed, skipped


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="print what would change; don't write")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--workspace", help="run for one workspace id")
    g.add_argument("--all-workspaces", action="store_true", help="run for every active workspace")
    args = p.parse_args()

    sk = secret_keeper.get()
    if not sk.enabled:
        print("LEADGEN_MASTER_KEY is not set or cryptography is missing.")
        print("Set it (e.g. `export LEADGEN_MASTER_KEY=...`) and re-run.")
        return 2

    platform_db.init_schema()
    if args.all_workspaces:
        targets = [w["id"] for w in platform_db.list_active_workspaces()]
        if not targets:
            print("no active workspaces found.")
            return 0
    else:
        ws = platform_db.get_workspace(args.workspace)
        if not ws:
            print(f"no workspace with id {args.workspace!r}.")
            return 2
        targets = [ws["id"]]

    total_changed = total_skipped = 0
    for wid in targets:
        print(f"=== workspace {wid} ===")
        changed, skipped = _run_for_workspace(wid, dry_run=args.dry_run, sk=sk)
        total_changed += changed
        total_skipped += skipped

    print()
    print(f"  dry_run : {args.dry_run}")
    print(f"  changed : {total_changed}")
    print(f"  skipped : {total_skipped} (already encrypted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
