#!/usr/bin/env python3
"""
One-shot migration tool for sensitive SQLite `settings` rows.

Two modes
=========
1. **MIGRATE** (default) — wrap plaintext rows using LEADGEN_MASTER_KEY
   from env so newly-stored secrets stop being plaintext. Already-encrypted
   rows are skipped. Run this once after first setting LEADGEN_MASTER_KEY.

2. **ROTATE**  (`--rotate --old-key <PREV>`) — decrypt every existing
   ciphertext row with the OLD key, then re-encrypt with LEADGEN_MASTER_KEY
   from env (the new key). This is the supported path for rotating the
   master key without losing history. Workflow::

       # 1) Pick a new key and stash the old one
       NEW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
       OLD="$LEADGEN_MASTER_KEY"          # whatever's currently in env
       echo "save NEW: $NEW"              # put it in ~/leadgen.env

       # 2) Update the env file + push + restart Pi (deploy.sh handles this)

       # 3) On the Pi (or on the laptop with the venv), with NEW exported:
       export LEADGEN_MASTER_KEY="$NEW"
       python -m ai_agency.scripts.encrypt_secrets --rotate --old-key "$OLD" --all-workspaces

The script is idempotent on the encrypted flag — re-running on already-
wrapped rows is a no-op in MIGRATE mode (and a useful audit-check in
ROTATE mode).

Usage
=====
::

    # Migrate plaintext rows in one workspace (or sweep all)
    python -m ai_agency.scripts.encrypt_secrets --workspace <id>
    python -m ai_agency.scripts.encrypt_secrets --all-workspaces
    python -m ai_agency.scripts.encrypt_secrets --all-workspaces --dry-run

    # Rotate the master key
    python -m ai_agency.scripts.encrypt_secrets --rotate --old-key <PREV> --all-workspaces
    python -m ai_agency.scripts.encrypt_secrets --rotate --old-key <PREV> --all-workspaces --dry-run

Side effects beyond the row updates: writes a `settings.rotated_master_key`
or `settings.migrated_to_encryption` audit entry so the dashboard can show
"all secrets encrypted under the current master key" badge.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `ai_agency` resolvable when run from repo root or from the
# scripts/ subdir alike.
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO))

from ai_agency import db, platform_db, secret_keeper  # noqa: E402

SENSITIVE_KEYS = list(db.SENSITIVE_KEYS)


def _keeper_with_key(key: str) -> "secret_keeper.SecretKeeper":
    """Construct a SecretKeeper explicitly keyed, bypassing the env lookup.
    Used by --rotate so the same process can decrypt with the OLD key while
    LEADGEN_MASTER_KEY in env points at the NEW key."""
    from cryptography.fernet import Fernet
    sk = secret_keeper.SecretKeeper.__new__(secret_keeper.SecretKeeper)
    sk._raw = key
    sk._fernet = Fernet(secret_keeper._derive_key(key)) if key else None
    return sk


def _run_for_workspace(
    workspace_id: str, *, dry_run: bool, sk_new, sk_old=None
) -> tuple[int, int]:
    """sk_new: encrypt-only keeper (uses LEADGEN_MASTER_KEY env).
    sk_old: optional decrypt-only keeper using --old-key for rotate mode.

    In MIGRATE (sk_old=None): already-encrypted rows are skipped.
    In ROTATE  (sk_old set): every encrypted row is first decrypted with
    sk_old, then re-wrapped with sk_new. Plaintext rows go through sk_new
    directly either way."""
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
            already_enc = any(
                v.startswith("enc:") for v in cur.values() if isinstance(v, str)
            )
        elif isinstance(cur, str):
            already_enc = cur.startswith("enc:")
        else:
            already_enc = False

        if already_enc and sk_old is None:
            # MIGRATE: leave already-wrapped rows alone.
            skipped += 1
            continue
        if sk_old is not None and already_enc:
            # ROTATE: decrypt with OLD key so the new wrap sees plaintext.
            cur = sk_old.unwrap(cur)

        new = sk_new.wrap(cur, field_paths=db._SENSITIVE_FIELD_PATHS.get(r["key"], ()))
        verb = "re-wrap" if (sk_old is not None and already_enc) else "wrap"
        if dry_run:
            print(f"  WOULD {verb} settings.{r['key']}")
        else:
            conn.execute(
                "UPDATE settings SET value=?, updated_at=? WHERE key=?",
                (json.dumps(new), db._now(), r["key"]),
            )
            past = "re-wrapped" if verb == "re-wrap" else "wrapped"
            print(f"  {past} settings.{r['key']}")
        changed += 1

    if not dry_run and changed:
        kind = (
            "settings.rotated_master_key"
            if sk_old is not None
            else "settings.migrated_to_encryption"
        )
        db.audit(kind, {"changed": changed, "by": "encrypt_secrets.py"})
    return changed, skipped


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="print what would change; don't write")
    p.add_argument("--rotate", action="store_true",
                   help="re-encrypt existing ciphertext: decrypt with --old-key, "
                        "re-wrap with LEADGEN_MASTER_KEY env (the new key)")
    p.add_argument("--old-key",
                   help="the PREVIOUS LEADGEN_MASTER_KEY; required with --rotate, "
                        "forbidden without it")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--workspace", help="run for one workspace id")
    g.add_argument("--all-workspaces", action="store_true",
                   help="run for every active workspace")
    args = p.parse_args()

    sk_new = secret_keeper.get()
    if not sk_new.enabled:
        print("LEADGEN_MASTER_KEY is not set (or cryptography is missing).")
        print("It's the NEW key for --rotate, or the only key for migration.")
        return 2

    sk_old = None
    if args.rotate:
        if not args.old_key:
            print("--rotate requires --old-key (the PREVIOUS LEADGEN_MASTER_KEY).")
            return 2
        sk_old = _keeper_with_key(args.old_key)
        print("rotating: --old-key (decrypt) → LEADGEN_MASTER_KEY env (encrypt)")
    elif args.old_key:
        print("--old-key is only valid with --rotate.")
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
        changed, skipped = _run_for_workspace(
            wid, dry_run=args.dry_run, sk_new=sk_new, sk_old=sk_old
        )
        total_changed += changed
        total_skipped += skipped

    print()
    print(f"  dry_run         : {args.dry_run}")
    print(f"  rotated (--old) : {sk_old is not None}")
    print(f"  changed         : {total_changed}")
    print(f"  skipped         : {total_skipped} (already encrypted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
