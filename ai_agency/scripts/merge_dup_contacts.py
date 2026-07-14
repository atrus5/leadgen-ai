#!/usr/bin/env python3
"""
One-shot migration tool: scan the `prospects` table for duplicate email
addresses across clients, create a canonical `email_contacts` row for
each, link the surviving prospects via `prospects.contact_id`, and
delete the leftover duplicate prospects (keeping their prospect_emails
+ replies history attached to the survivor by reassigning prospect_id).

When to run this
================
Once per workspace, right after upgrading from the per-(client,
external_id) dedup model to the new global email_contacts model. Each
workspace has its own SQLite file, so pick one explicitly or sweep all of
them:

    python -m ai_agency.scripts.merge_dup_contacts --workspace <id>
    python -m ai_agency.scripts.merge_dup_contacts --all-workspaces
    python -m ai_agency.scripts.merge_dup_contacts --all-workspaces --dry-run

After it runs, the nightly hunters and the sender both consult
`email_contacts.email` before any insert or send, so a prospect who
shows up under a second client gets linked to the existing contact
instead of being emailed twice.

Selection rules
==============
* Lowercase email comparison; whitespace-trimmed.
* Surviving prospect: max(s.score) DESC, max(added_at) DESC tiebreak.
* The survivor absorbs the contact_id; duplicates are reassigned or
  deleted, depending on the merge mode (default: hard-delete).
* prospect_emails and replies for a deleted duplicate are reassigned
  to the surviving prospect's id (FK ON DELETE SET NULL on replies
  is honoured so we lose no audit trail if reassignment fails).
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO))

from ai_agency import db, platform_db  # noqa: E402


def _run_for_workspace(workspace_id: str, *, dry_run: bool) -> None:
    db.use_workspace(workspace_id)
    conn = db.get_db()

    rows = conn.execute(
        "SELECT id, contact_email, client_id, score, added_at, business_name FROM prospects "
        "WHERE contact_email IS NOT NULL AND contact_email <> '' "
        "ORDER BY contact_email"
    ).fetchall()

    by_email = collections.defaultdict(list)
    for r in rows:
        norm = (r["contact_email"] or "").strip().lower()
        if not norm:
            continue
        by_email[norm].append(r)

    groups = {e: ps for e, ps in by_email.items() if len(ps) > 1}
    if not groups:
        print("no duplicate emails found \u2014 nothing to merge.")
        return

    print(f"found {len(groups)} email(s) with >=2 prospects:")
    inserted_contacts = 0
    reassigned_rows = 0
    deleted_rows = 0
    for email, prospects in groups.items():
        # Survivor = highest score, ties broken by latest added_at. No
        # negation needed: max() already picks the largest tuple, and
        # ISO-8601 timestamps sort correctly as plain strings (ascending =
        # chronological), so (score, added_at) in natural ascending order
        # is exactly "highest score, latest timestamp wins".
        survivor = max(
            prospects,
            key=lambda r: (r["score"] or 0, r["added_at"] or ""),
        )
        survivor_id = survivor["id"]
        # Insert or look up the contact row.
        existing = conn.execute(
            "SELECT id FROM email_contacts WHERE email=?", (email,)
        ).fetchone()
        if not existing:
            import uuid
            cid = uuid.uuid4().hex
            if dry_run:
                print(f"  WOULD create email_contacts[email={email!r}, id={cid[:8]}]")
            else:
                conn.execute(
                    "INSERT INTO email_contacts(id, email, business_name, "
                    "first_seen_at, last_seen_at, first_source) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        cid, email, survivor["business_name"],
                        survivor["added_at"] or db._now(),
                        db._now(),
                        "merge_dup_contacts.py",
                    ),
                )
                db.audit("email_contact.created_from_merge", {"email": email})
            inserted_contacts += 1
        else:
            cid = existing["id"]

        # Link survivors + reassign + delete duplicates.
        for p in prospects:
            if p["id"] == survivor_id:
                if dry_run:
                    print(f"    WOULD link survivor {survivor_id[:8]} \u2192 {cid[:8]}")
                else:
                    conn.execute(
                        "UPDATE prospects SET contact_id=? WHERE id=?", (cid, survivor_id)
                    )
                continue
            # Reassign prospect_emails + replies before deleting.
            if dry_run:
                print(f"    WOULD reassign emissions+replies of dup {p['id'][:8]} \u2192 survivor")
                print(f"    WOULD DELETE dup {p['id'][:8]} ({(p['business_name'] or '?')!r})")
            else:
                conn.execute(
                    "UPDATE prospect_emails SET prospect_id=? WHERE prospect_id=?",
                    (survivor_id, p["id"]),
                )
                conn.execute(
                    "UPDATE replies SET prospect_id=? WHERE prospect_id=?",
                    (survivor_id, p["id"]),
                )
                conn.execute("DELETE FROM prospects WHERE id=?", (p["id"],))
                db.audit(
                    "prospect.merged_into_contact",
                    {"deleted_prospect_id": p["id"], "kept_prospect_id": survivor_id, "email": email},
                )
                reassigned_rows += 1
                deleted_rows += 1

    if not dry_run:
        db.audit(
            "email_contacts.merge_run",
            {
                "groups_merged": len(groups),
                "contacts_created": inserted_contacts,
                "duplicates_deleted": deleted_rows,
            },
        )

    print()
    print(f"  dry_run : {dry_run}")
    print(f"  groups  : {len(groups)}")
    print(f"  contacts: {inserted_contacts} created")
    print(f"  deleted : {deleted_rows} duplicate prospects")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--workspace", help="run for one workspace id")
    g.add_argument("--all-workspaces", action="store_true", help="run for every active workspace")
    args = p.parse_args()

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

    for wid in targets:
        print(f"=== workspace {wid} ===")
        _run_for_workspace(wid, dry_run=args.dry_run)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
