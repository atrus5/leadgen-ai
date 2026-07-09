# LeadGen AI — Operating Reference

This is the long-form reference for the LeadGen AI agency stack. The HTML
playbook at `/playbook/` is the at-a-glance manual; this document is the
canonical reference for operators who want to extend or debug the system.

## 1. Where everything lives

```
/home/your-user/leadgen-ai/
  index.html                              # marketing landing (existing)
  ai_agency/
    app.py                                # Flask + scheduler entrypoint
    config.py                             # settings + niches loaders
    db.py                                 # SQLite schema + helpers
    requirements.txt
    Caddyfiles.txt                        # reverse proxy config
    data/agency.db                        # all durable state (SQLite, WAL)
    config/
      settings.json                       # sample settings to fill in
      niches.json                         # 24 niches of email DNA
    public/
      index.html                          # landing page
      wizard.html                         # multi-step setup wizard
      playbook/
        index.html                        # docs landing
        01-domain.html … 08-recipes.html  # step-by-step subpages
    hunters/
      apollo.py                           # Apollo.io REST adapter
      google_places.py                    # Google Places (New) adapter
    outreach/
      templates.py                        # niche DNA renderer (pure-Python)
      generator.py                        # templates + Llama 3 rewrites
      sender.py                           # SMTP with daily quota
      reply_parser.py                     # IMAP poll → classify
      forwarder.py                        # HOT → client email
    scheduler/
      jobs.py                             # APScheduler wiring
    docs/
      MASTER_SALES_DOC.txt                # agency sales contract template
      PLAYBOOK.md                         # (this file)
    scripts/
      report.sh                           # weekly revenue report
      monitor.sh                          # heartbeat check
      backup.sh                           # tar + DB snapshot
      bootstrap.sh                        # one-shot install
```

## 2. SQLite schema (the source of truth)

Schema is created idempotently by `db.init_schema()` on every app boot.
`data/agency.db` is the canonical state; columns intentionally narrow and
explicit so you can `sqlite3 data/agency.db ".schema"` and read it.

| Table              | Purpose                                                                       |
| ------------------ | ----------------------------------------------------------------------------- |
| settings           | singleton-style key/value JSON                                               |
| clients            | the businesses paying us                                                     |
| prospects          | the leads found for each client                                               |
| prospect_emails    | outbound messages actually sent                                               |
| replies            | inbound replies with classified intent + forward state                        |
| warmups            | domains in warmup (with from_address, start_date, target_daily)              |
| warmup_log         | one row per warmup_id+day (idempotent)                                        |
| hunt_runs          | one row per hunter invocation per client with found/inserted counts           |
| job_runs           | one row per scheduler tick with started/finished + summary/error              |
| blacklist          | emails and domains we never contact again                                     |
| sender_quotas      | per (from_address, day) sent count                                           |
| audit              | any external action for postmortems                                           |

Quotas and idempotency sit in this table architecture: sender_quotas pins
daily send counts; the unique constraint `(client_id, external_id)` on
prospects stops the same business from being inserted twice by Apollo+Places.

## 3. The scheduler

APScheduler runs four jobs, each guarded by `job_runs` so a cold restart
won't double-fire within the same day:

| Name          | When                  | What                                              |
| ------------- | --------------------- | -------------------------------------------------- |
| nightly_hunt  | nightly_hunt_hour:10  | Apollo + Places for every active client            |
| morning_brief | morning_brief_hour:05 | email operator the daily summary                   |
| reply_poller  | every 15 min          | IMAP → classify → persist → forward HOTs           |
| warmup_tick   | 23:50 daily           | increment warmup day index, complete at day=14     |

`scheduler.enabled` in settings is the master switch. Disable it on staging.

## 4. API surface (abridged)

All `/api/*` routes return JSON of the shape `{ok: true, data: {...}}` or
`{ok: false, error: "..."}`. The honest test is `curl /healthz` — if it
returns `{ok:true}` the database is reachable and the scheduler is wired.

```
GET    /api/dashboard/summary           counter wall
GET    /api/clients                     list clients
POST   /api/clients                     create
PUT    /api/clients/<id>                update
DELETE /api/clients/<id>                pause (soft)
GET    /api/clients/<id>/prospects      prospects for one client
GET    /api/prospects                   list, paginated
POST   /api/prospects                   create manually
DELETE /api/prospects/<id>
GET    /api/warmups                     list warmup domains
POST   /api/warmups                     start a warmup
GET    /api/replies?intent=HOT
POST   /api/hunt                        run hunters (all or one)
POST   /api/brief                       manual morning brief
POST   /api/check-inbox                 manual reply poll
POST   /api/jobs/run body={name}        run a named job on demand
POST   /api/email/send body={prospect_id, step}
GET    /api/email/remaining-quota
GET    /api/niches
GET    /api/settings
PUT    /api/settings body={key,value}
POST   /api/settings/test-smtp
POST   /api/settings/test-imap
```

## 5. Settings keys

Stored in the SQLite `settings` table. Read via `GET /api/settings`. Update
via `PUT /api/settings` with `{key, value}`.

* `agent` — owner details, SMTP/IMAP credentials, morning-brief hour, etc.
* `apis` — Apollo and Google Places keys + toggles
* `warmup` — the 14-day plan + max-daily-after
* `scheduler` — master toggle + per-job toggles
* `hot_forward` — body template customization for forwarding to clients

## 6. Delivery best practices

* Don't promise day-one campaigns. The 14-day warmup is a feature, not friction.
* Generate from your own gmail first when testing the SMTP/IMAP wiring. Loop back to your domain once it's stable.
* Always send the first 5 emails to yourself. If they land in spam you haven't set DKIM correctly.
* Per-niche cards (send a plumbers plugins for plumbers opener, dentists for dentist) perform better than a generic intro — that's why the DNA has 24 niches.

## 7. Failure modes

| Failure                          | Likely cause                                                  |
| -------------------------------- | ------------------------------------------------------------- |
| Send returns quota_exceeded      | Today's quota is full. Wait or raise `warmup.max_daily_after`.|
| Send returns duplicate_step      | Already sent this step in the last 7 days. Pick a new prospect.|
| SMTP auth errors                 | App password rotated. Run the SMTP test from the wizard.      |
| Brief never arrives              | `agent.morning_brief_to` is empty. Set it.                    |
| Hunters insert 0                 | API key expired. Or the city field is too narrow.             |
| IMAP fetches empty               | Your mailbox doesn't allow IMAP. Re-enable in the provider.  |
| FORWARD shows `no_client_email`  | The client record's `contact_email` is empty. Fix it.         |

## 8. How to add a new niche

1. Append an entry under `niches:` in `config/niches.json`.
2. Restart the Flask process so `load_niches()` re-reads it.
3. The hunter will pick the new niche from `client.niche` automatically.

## 9. How to add a new lead source

Create a module under `hunters/<source>.py` that exposes:

```
enabled() -> bool
to_prospect_dict(raw, client) -> {...}
run_for_client(client) -> {found, inserted}
```

Then call it from `scheduler/jobs.py::nightly_hunt`.

## 10. How to add a new job

Add a function to `scheduler/jobs.py` taking `_: datetime | None = None` and
returning a dict. Wire it into `install()` with `sched.add_job(...)` and
expose it through `/api/jobs/run`.

## 11. Backup + restore

`scripts/backup.sh` produces a tar.gz snapshot at
`$HOME/agency_backups/agency_system_snapshot_<TS>.tar.gz` containing code,
config, and the SQLite DB. Restore with:

```
cd /home/your-user
tar -xzf agency_backups/agency_system_snapshot_YYYY-MM-DD_HHMMSS.tar.gz
```

## 12. Hard rules

* Never email a blacklisted address. The `blacklist` table is the contract.
* Always respect the daily quota. The scheduler will refuse to overflow.
* Use the niche DNA. Generic openings convert lower and waste quota.
* Hold the agency contract to a HOT-only billing metric. Don't bill for non-HOT.
* DMARC `p=none` for week 3+ before moving to `p=quarantine`.
