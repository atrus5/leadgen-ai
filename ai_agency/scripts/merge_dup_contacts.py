#!/usr/bin/env python3
"""
One-shot migration tool: scan the `prospects` table for duplicate email
addresses across clients, create a canonical `email_contacts` row for
each, link the surviving prospects via `prospects.contact_id`, and
delete the leftover duplicate prospects (keeping their prospect_emails
+ replies history attached to the survivor by reassigning prospect_id).

When to run this
================
Once, right after upgrading from the per-(client, external_id) dedup
model to the new global email_contacts model:

    python -m ai_agency.scripts.merge_dup_contacts            # do it
    python -m ai_agency.scripts.merge_dup_contacts --dry-run  # preview

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

from ai_agency import db  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db.init_schema()
    db.ensure_default_settings()
    conn = db.get_db()

    rows = conn.execute(
        "SELECT id, email, client_id, score, added_at FROM prospects "
        "WHERE email IS NOT NULL AND email <> '' "
        "ORDER BY email"
    ).fetchall()

    by_email = collections.defaultdict(list)
    for r in rows:
        norm = (r["email"] or "").strip().lower()
        if not norm:
            continue
        by_email[norm].append(r)

    groups = {e: ps for e, ps in by_email.items() if len(ps) > 1}
    if not groups:
        print("no duplicate emails found \u2014 nothing to merge.")
        return 0

    print(f"found {len(groups)} email(s) with >=2 prospects:")
    inserted_contacts = 0
    reassigned_rows = 0
    deleted_rows = 0
    for email, prospects in groups.items():
        # Survivor = highest score, ties broken by latest added_at.
        survivor = max(
            prospects,
            key=lambda r: (
                -(r["score"] or 0),
                -(int((r["added_at"] or "1970-01-01").replace("-", "").replace(":", ""))),
            ),
        )
        survivor_id = survivor["id"]
        # Insert or look up the contact row.
        existing = conn.execute(
            "SELECT id FROM email_contacts WHERE email=?", (email,)
        ).fetchone()
        if not existing:
            import uuid
            cid = uuid.uuid4().hex
            if args.dry_run:
                print(f"  WOULD create email_contacts[email={email!r}, id={cid[:8]}]")
            else:
                conn.execute(
                    "INSERT INTO email_contacts(id, email, business_name, "
                    "first_seen_at, last_seen_at, first_source) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        cid, email, survivor.get("business_name"),
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
                if args.dry_run:
                    print(f"    WOULD link survivor {survivor_id[:8]} \u2192 {cid[:8]}")
                else:
                    conn.execute(
                        "UPDATE prospects SET contact_id=? WHERE id=?", (cid, survivor_id)
                    )
                continue
            # Reassign prospect_emails + replies before deleting.
            if args.dry_run:
                print(f"    WOULD reassign emissions+replies of dup {p['id'][:8]} \u2192 survivor")
                print(f"    WOULD DELETE dup {p['id'][:8]} ({p.get('business_name', '?')!r})")
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

    if not args.dry_run:
        db.audit(
            "email_contacts.merge_run",
            {
                "groups_merged": len(groups),
                "contacts_created": inserted_contacts,
                "duplicates_deleted": deleted_rows,
            },
        )

    print()
    print(f"  dry_run : {args.dry_run}")
    print(f"  groups  : {len(groups)}")
    print(f"  contacts: {inserted_contacts} created")
    print(f"  deleted : {deleted_rows} duplicate prospects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
