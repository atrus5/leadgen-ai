# START HERE — bring up the LeadGen AI operator dashboard

`index.html` at the project root is the operator dashboard. You're seeing
this README because either the dashboard is blank/404, you've just
cloned the repo, or you've changed something upstream of it (backend,
auth, Caddy, OAuth secrets). This file lists every check between
"fresh VPS" and "operator dashboard responsive in the browser".

> Composed session-by-session against `ai_agency/`. Where something
> here disagrees with code, treat the **code** as truth and update this
> doc. Where the code is wrong, fix the code rather than working around
> it.

---

## 0. Goal & your role

LeadGen AI is an **agency tool**. You (the operator) run the app on
your server. Your clients (local service businesses — roofers,
plumbers, dentists, etc.) show up via `/run-audit` and `/new-lead`
webhook leads. Cold-email sequences go out from your warmed-up
domains. Index.html is where **you** operate the system (read replies,
label intent, push follow-ups, send briefs).

Nothing in this README is for a customer. If you're handing the app
to a non-technical operator, walk them through Sections 1–4 once and
forward them Sections 5+ only when something breaks.

---

## 1. Quick "is anything broken?" health check

Before doing anything, run these from any host with shell + curl:

```bash
# 1) Backend reachable at all:
curl -fsS https://your.domain.tld/healthz | jq .
#   expect: {"ok": true, "data": {"db": true, ...}}

# 2) Auth gate not stuck on "not installed":
curl -fsS https://your.domain.tld/api/auth/status | jq .
#   expect: {"ok": true, "data": {"installed": true, "authed": false}}

# 3) Webhook secret exists (you need this to integrate inbound forms):
#    This one is logged-in only — see Section 4 to obtain it.

# 4) Local row counts (smoke proxy):
curl -fsS https://your.domain.tld/api/dashboard/summary | jq .
#   expect: clients, prospects, contacted, hot_replies, forwarded_leads,
#   warmups_active — all numbers, no auth required
```

If any of those 1–4 return non-2xx, **stop and triage below in §9
before opening index.html**. A 200 with `installed: false` means the
orchestrator never finished first-boot. A 401 on `/api/dashboard/summary`
means the auth gate's exempt-list miscounts.

---

## 2. Pre-flight (one-time, before you ever boot)

Make sure the **host itself** has these; everything else cascades.

| Item                                  | What's needed                                                  |
|---------------------------------------|---------------------------------------------------------------|
| OS                                   | Linux (Ubuntu 22.04+ works). macOS works for dev.             |
| Python                               | 3.10+ in PATH as `python3`                                     |
| DNS                                  | A real domain pointing A/AAAA at this host's public IP        |
| Ports                                | TCP 80 + 443 open from the public Internet (Caddy terminations)|
| Reverse-proxy                        | Caddy v2 installed (`/usr/local/bin/caddy` or `apt install`) |
| LLM (optional but recommended)        | Ollama on `localhost:11434` with the model in `apis.ollama_model` (default `llama3`) pulled. Without it, intent classification & email generation degrade to keyword-only heuristics. |
| Outbound mail                        | At least ONE warmed-up sending domain (DNS-aligned SPF/DKIM/DMARC) — the `/wizard` step 1 expects a domain you own. Buy/use cheap: 1yr .com + Google Workspace or Zoho at $1/mo. |
| Time budget                          | First boot end-to-end is ~30 min including TLS + DNS + warming. Plan accordingly. |

DNS needs **at least 30 min** of TTL cushion for some registrars. Plan
to set the A record the night before if you care about ACME.

---

## 3. Backend boot (the orchestrator)

### 3.1 Env vars (production)

Write to `/etc/leadgen.env` on the host:

```bash
LEADGEN_FLASK_SECRET='<32+ random bytes, hex or url-safe>'   # session signing — stays valid across restarts
LEADGEN_MASTER_KEY='<strong passphrase>'                    # settings-at-rest encryption
```

If you don't set these, the orchestrator runs in **dev mode**: it
generates a random per-process session key (every restart logs
everyone out) and stores SMTP/IMAP/API passwords **in plaintext**
in `data/agency.db`. The latter is the bigger pain — set the master
key. Once you have it, migrate the existing plaintext:

```bash
cd /home/paul/leadgen-ai
PYTHONPATH=$PWD LEADGEN_MASTER_KEY='<same value as /etc/leadgen.env>' \
  python -m ai_agency.scripts.encrypt_secrets
```

Run this **once** per environment. The script is idempotent on the
encrypted flag — re-running on a clean DB is a no-op.

### 3.2 First boot

```bash
cd /home/paul/leadgen-ai
bash ai_agency/scripts/bootstrap.sh
```

That creates `.venv`, installs `requirements.txt`, initialises
`data/agency.db`, writes `ai_agency/config/settings.json`, and starts
`ai_agency.app` on `0.0.0.0:5000` in the foreground. **For real
production** wrap it in a systemd unit (or supervisor of your choice):

```ini
[Unit]
Description=LeadGen AI orchestrator
After=network-online.target

[Service]
WorkingDirectory=/home/paul/leadgen-ai
EnvironmentFile=/etc/leadgen.env
ExecStart=/home/paul/leadgen-ai/.venv/bin/python -m ai_agency.app
Restart=on-failure
User=leadgen
Group=leadgen

[Install]
WantedBy=multi-user.target
```

Then `systemctl daemon-reload && systemctl enable --now leadgen`.

### 3.3 Capture the install token

On the **first** boot after a fresh `data/agency.db`, the orchestrator:

1. Logs a one-time 12-char setup token to `LOG.warning` (also writes
   it to `data/last_install_token.txt` mode 0600).
2. Generates a 32-char `webhook_secret` (also logged `LOG.warning`).

Copy both into a password manager. The install token is **valid for
30 minutes** and is consumed by Section 4. The webhook secret is
persistent until rotated.

If you boot again without installing a password, the same token stays
valid (renewed on each unauthenticated call to `/api/auth/install-token`).
If `data/agency.db` is deleted, a fresh token is generated.

---

## 4. Set the dashboard password (first-boot gate)

The dashboard at `/` will refuse to render until `auth.password_hash`
exists in `settings.auth`. You set it via either:

- **The wizard** at `https://your.domain.tld/wizard` (when its Step 0
  lands; until then use curl below).
- **`curl` against the API** (always available):

```bash
# Get the (still-valid) one-time token:
TOKEN=$(curl -fsS https://your.domain.tld/api/auth/install-token \
  | jq -r .data.setup_token)

# Submit a strong password (≥ 8 chars, ≥1 number or symbol):
curl -fsS -X POST https://your.domain.tld/api/auth/install \
  -H "Content-Type: application/json" \
  -d "{\"setup_token\": \"$TOKEN\",
       \"password\": \"...your-strong-password...\",
       \"password_confirm\": \"...your-strong-password...\"}"
```

A 200 means `auth.installed == true`. A 422 with
`invalid_or_expired_setup_token` means you waited past 30 min — fetch
a fresh one and try again.

After install, `data/last_install_token.txt` is left in place for
audit. Delete it manually if you want.

> **Operator hygiene**: never paste this token into Slack/Slack DM.
> Anyone who sees it can install a password and re-flash the dashboard.

---

## 5. Run `/wizard` end-to-end

The wizard lives at `/wizard` and is served by the Flask backend. Steps:

| Step             | What to fill in                                           | Notes                                                       |
|------------------|------------------------------------------------------------|-------------------------------------------------------------|
| 1 — Domain       | brand, sender-domain, from-name                           | Sender domain MUST be a real registered, warmed domain.     |
| 2 — DNS Records  | (read-only — copy-paste SPF/DKIM/DMARC into your DNS host) | Doing this wrong = 90% spam rate, no triage, just delete the domain and try again.|
| 3 — Mailbox      | Choose: **Gmail/Workspace/Zoho/SES** — pick the preset that matches your provider | Preset auto-fills step 4 with sane defaults.                |
| 4 — SMTP/IMAP    | smtp_host, smtp_user, smtp_pass, imap_host, imap_user, imap_pass. Use **app passwords**, not your real account password.| Test buttons at the bottom of the step will round-trip.  |
| 5 — API Keys     | Apollo (optional), Google Places (optional), Ollama host. **Tracking base_url** needs to be set to your `https://your.domain.tld` for the tracking pixel to work.| Ollama host is `http://localhost:11434` if co-located.        |
| 6 — Schedule     | `nightly_hunt_hour` (default 2am), `morning_brief_hour` (default 8am), `reply_poller` (default every 15 min), toggle scheduler.enabled.| First the scheduler is disabled so you can test send-by-hand. |

The wizard saves on every change (`setKey` listener). Don't worry about
losing progress.

After Step 4 you'll have valid SMTP. Verify:

```bash
curl -X POST https://your.domain.tld/api/settings/test-smtp
# expect: {"ok": true, "data": {"smtp": "ok"}}
```

After Step 5 with `tracking.base_url` set, verify the tracking pixel:

```bash
# 1) login as the operator:
curl -c /tmp/jar https://your.domain.tld/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"password":"...your-strong-password..."}'

# 2) get a sample pixel URL:
curl -fsS -b /tmp/jar https://your.domain.tld/api/auth/webhook/url | jq .
#   expect: data.run_audit_url + data.new_lead_url + secret_masked

# 3) probe the pixel — should be 200 image/gif even on
#    unknown tracking_id (server returns 200 + bytes always to keep
#    mail-gateway scanners from re-firing):
curl -fsSI "https://your.domain.tld/t/o/test.gif" | head
#   expect: HTTP/2 200, Content-Type: image/gif
```

---

## 6. Open `index.html` for the first time

### 6.1 URL

`https://your.domain.tld/` — the Flask backend serves the
operator-dashboard HTML. If you see a 404 here, the most likely
reason is that Flask's `/` route serves `ai_agency/public/index.html`
and **the operator dashboard lives at the project root, not under
`ai_agency/public/`**. Two clean fixes:

- Move `index.html` to `ai_agency/public/index.html`, OR
- Update `ai_agency/app.py`'s `@app.route("/")` to point at
  `BASE_DIR.parent / "index.html"` instead of `BASE_DIR / "public" / "index.html"`.

Pick one before the rest of this section will work.

### 6.2 Auth screen

The dashboard renders an auth screen first with email + password
fields. **The email field is no longer used** — hide it client-side
if you want, the backend only cares about the password. Authentication
flow (after my recent PR):

1. JS calls `GET /api/auth/status` — gets `installed: true, authed: false`.
2. User types password, clicks "Sign In".
3. JS posts to `/api/auth/login` with `{"password": "..."}`.
4. On 200, JS sets a session cookie, calls `showApp(); fbLoad(); renderAll()`.
5. `fbLoad()` checks `localStorage` for cached state — first run shows
   "Offline — using local data" toast (this is normal; it hasn't missed
   anything yet).

### 6.3 First-render expectations

Right after the auth screen, the dashboard renders **empty** —
that's fine. Real data shows up after the wizard configures a client
and the scheduler runs `nightly_hunt`. Until then:

```bash
# Verify in-page data loads:
curl -fsS -b /tmp/jar https://your.domain.tld/api/clients | jq .
# expect: ok: true, data: [...]
```

The dashboard's `_wiredLoginForm()` block in `initAuth()` is what
posts the password. If a Sign In click does nothing, open DevTools →
Console and verify there are no 401s on `/api/auth/status`.

---

## 7. Inbound webhooks (`/run-audit` and `/new-lead`)

Both endpoints are gated by `?token=<webhook_secret>` (NOT session
auth — they're called by external pipelines). To get the URLs:

```bash
curl -fsS -b /tmp/jar https://your.domain.tld/api/auth/webhook/url | jq .
```

Hand the `run_audit_url` and `new_lead_url` to whoever wires your
landing-page form or reply pipeline. Test:

```bash
# Reject without token:
curl -i -X POST https://your.domain.tld/run-audit \
  -H "Content-Type: application/json" -d '{"business_name":"X","email":"x@y.com"}'
# expect: HTTP/2 401

# Accept with token:
curl -fsS -X POST "https://your.domain.tld/run-audit?token=$WEBHOOK_SECRET" \
  -H "Content-Type: application/json" -d '{"business_name":"X","email":"x@y.com"}'
# expect: ok: true
```

If you need to **rotate** the webhook secret: there's no UI yet. Use
`db.set_setting("webhook_secret", {"value": "<new>", "issued_at": "..."})`
via the SQLite shell, then `config.load_settings.cache_clear()` on
the running process.

---

## 8. Going live (before you flip the switch to paid customers)

- **HTTPS in front**: copy `ai_agency/Caddyfiles.txt` to
  `/etc/caddy/Caddyfile`, replace `agency.example.com` with your real
  domain, `caddy validate --config /etc/caddy/Caddyfile`, then
  `caddy reload`. Caddy provisions Let's Encrypt on first request.
- **Backups**: `ai_agency/scripts/backup.sh` exists. Wire it via
  systemd timer or cron — daily is fine, weekly is acceptable. Send
  copies off-box (rsync, rclone, b2, s3).
- **Monitoring**: at minimum, hit `https://your.domain.tld/healthz`
  every 5 min from a separate host (cron + curl). Wire PagerDuty if
  you have it. Make sure `data/agency.db-wal` size stays under a few
  hundred MB — if it grows, the scheduler is choking on something.
- **Rate-limit the tracking pixel**: enforced by `ai_agency/Caddyfiles.txt`
  at 60 r/s per source IP. If you have higher traffic, raise the
  limit. If you have mail-gateway scanners hammering harder, add a
  UA block list.
- **Disable scheduler until you've smoke-tested**:
  `scheduler.enabled=False` in the wizard. Enable it only after one
  happy-path version of: hunt runs → emails queued → sent → reply
  arrives → classified HOT → forwarded to client.
- **One-shot dedup migration** (if upgrading an older install):
  `python -m ai_agency.scripts.merge_dup_contacts --dry-run`
  first, then the same command without `--dry-run` to apply.

---

## 9. Troubleshooting the dashboard

If the page is blank or stuck on auth, walk this list in order.

### 9.1 "I just see a black screen with ⚡ Loading your data..."

Means `initAuth()` started but the Supabase call failed (when
configured) or the Flask fallback can't reach `/api/auth/status`.

- **Supabase not configured at all** (the hardcoded URL/key on
  `index.html` line ~947): the dashboard falls back to local-only
  mode and reads from `localStorage["lgs5"]`. First-time users will
  see "⚠️ Offline — using local data" toast. Click around for a
  cycle; afterwards everything's in `localStorage`.
- **Supabase configured but unreachable**: check DevTools → Network
  for the failed request. Bad URL/anon key returns 401.
- **Flask unreachable but Supabase configured**: less likely; the
  `_SB_READY` constant is hardcoded `true`. If your Supabase project
  was deleted, you have to either restore it or patch `_SB_READY=false`
  in `index.html`.

### 9.2 "Auth screen renders and refresh status greys out"

Means orchestrator is up but auth is not installed.

```bash
# Re-fetch the install token + install:
TOKEN=$(curl -fsS https://your.domain.tld/api/auth/install-token \
  | jq -r .data.setup_token)
curl -fsS -X POST https://your.domain.tld/api/auth/install \
  -H "Content-Type: application/json" \
  -d "{\"setup_token\":\"$TOKEN\",\"password\":\"...\",\"password_confirm\":\"...\"}"
```

Or just nuke `data/agency.db` and re-boot — fresh install token issued.

### 9.3 "I sign in and get bounced back to the auth screen"

Almost always a session-cookie mismatch. With Caddy terminated TLS:

- `agent.tracking.base_url` must be set to `https://your.domain.tld/`
  (no trailing slash, no path). Otherwise `SESSION_COOKIE_SECURE=True`
  and the browser silently drops the cookie.
- If you just rotated `LEADGEN_FLASK_SECRET` mid-session, every
  browser in flight is now invalid. Re-login.

### 9.4 "Buttons work but lists are empty"

Scheduler hasn't run yet, or its jobs failed. Check:

- `db.set_setting("scheduler", {"enabled": True, "run_nightly_hunt": True, ...})` then
  `config.load_settings.cache_clear()`.
- `GET /healthz` returns `{"sched": true}` (lowercase field name — adapt
  to whatever the current schema is).
- `audit` table: `SELECT kind, created_at FROM audit WHERE kind LIKE 'apollo.%' OR kind LIKE 'google_places.%' ORDER BY created_at DESC LIMIT 10;`

### 9.5 "I get 415 on every POST"

The api-before_request middleware requires `Content-Type: application/json`
on every mutating endpoint. `curl` users must add
`-H "Content-Type: application/json"`; JS callers should already be
doing this if they use `fetch(..., {method: 'POST', headers: {...}})`.

---

## 10. Smoke test (one-liner)

When in doubt, run the project-level regression pack:

```bash
cd /home/paul/leadgen-ai
python -m venv /tmp/leadgen-smoke && source /tmp/leadgen-smoke/bin/activate
pip install -r ai_agency/requirements.txt
PYTHONPATH=$PWD LEADGEN_FLASK_SECRET=test LEADGEN_MASTER_KEY=test \
  python -m ai_agency.tests.test_smoke
```

Expect 18 passing, 0 failing.

If a test is now failing that you didn't change, Section 9 above
plus `git log -p ai_agency/tests/test_smoke.py` will usually show you
what state it expects.

---

## 11. Where to go next

- **`ai_agency/docs/PLAYBOOK.md`** — operator narrative for what to
  do when a HOT reply arrives.
- **`ai_agency/docs/MASTER_SALES_DOC.txt`** — sales pitch + agreement
  template (not technical).
- **`ai_agency/Caddyfiles.txt`** — the Caddy config you should drop
  on the host (edit `agency.example.com` first).
- **`ai_agency/scripts/`** — bootstrap.sh, backup.sh, monitor.sh,
  report.sh, encrypt_secrets.py, merge_dup_contacts.py.

Good luck. Don't trust the buyer on day 1.
