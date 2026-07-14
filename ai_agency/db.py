"""
SQLite schema + helpers for the LeadGen AI agency system.

One SQLite file PER WORKSPACE (tenant) at `data/workspaces/<id>/agency.db`.
WAL enabled for concurrent read while writing. Every table below is
implicitly scoped to "whichever workspace is current for this thread" —
callers never pass a workspace_id to any of these functions; instead,
`db.use_workspace(workspace_id)` is called once at the top of each Flask
request (see app.py's before_request) or once per iteration of a scheduler
sweep (see scheduler/jobs.py), and every function in this module resolves
against that. See platform_db.py for the separate, genuinely-singleton
database holding workspaces/users/invites/platform admins.

Tables:
  settings           - key/value JSON store (singleton row per key)
  clients            - agency clients (i.e. the businesses paying US for leads)
  prospects          - end-of-funnel prospects we will cold-email per client
  prospect_emails    - outbound messages actually sent (rate-limit + log)
  replies            - inbound replies, classified intent, forwarding state
  warmups            - domains being warmed up before sending campaigns
  warmup_log         - daily send counts per warmup domain
  hunt_runs          - record of each nightly hunt (results, errors)
  job_runs           - scheduler self-check (idempotency + debugging)
  blacklist          - emails/domains we never contact again
  sender_quotas      - daily send count per from-address
  audit              - any external action (api call, smtp, imap fetch)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACES_DIR = DATA_DIR / "workspaces"
# Pre-multi-tenant single DB path. Only referenced by
# scripts/migrate_to_workspace.py to find + move an existing install's data
# — nothing else should read or write this path anymore.
LEGACY_DB_PATH = DATA_DIR / "agency.db"

_LOCAL = threading.local()
_initialized_workspaces: set[str] = set()
_init_lock = threading.Lock()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    niche TEXT NOT NULL,
    city TEXT,
    region TEXT,
    country TEXT,
    contact_name TEXT,
    contact_email TEXT,
    retainer_cents INTEGER DEFAULT 0,
    start_date TEXT,
    notes TEXT,
    status TEXT DEFAULT 'active',
    days_per_week_target INTEGER DEFAULT 5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS clients_status_idx ON clients(status);
CREATE INDEX IF NOT EXISTS clients_niche_idx ON clients(niche);

CREATE TABLE IF NOT EXISTS prospects (
    id TEXT PRIMARY KEY,
    client_id TEXT REFERENCES clients(id) ON DELETE SET NULL,
    business_name TEXT NOT NULL,
    niche TEXT NOT NULL,
    city TEXT,
    region TEXT,
    country TEXT,
    website TEXT,
    phone TEXT,
    contact_name TEXT,
    contact_email TEXT,
    contact_title TEXT,
    source TEXT,
    external_id TEXT,
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'new',
    notes TEXT,
    added_at TEXT NOT NULL,
    last_contacted_at TEXT,
    last_replied_at TEXT,
    UNIQUE(client_id, external_id)
);

CREATE INDEX IF NOT EXISTS prospects_client_idx ON prospects(client_id);
CREATE INDEX IF NOT EXISTS prospects_status_idx ON prospects(status);
CREATE INDEX IF NOT EXISTS prospects_niche_idx ON prospects(niche);

CREATE TABLE IF NOT EXISTS prospect_emails (
    id TEXT PRIMARY KEY,
    prospect_id TEXT NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    client_id TEXT REFERENCES clients(id) ON DELETE SET NULL,
    step TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    from_address TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    message_id TEXT,
    status TEXT DEFAULT 'queued',
    tracking_id TEXT  -- 11-char URL-safe token; NULL when tracking disabled
);

CREATE INDEX IF NOT EXISTS prospect_emails_prospect_idx ON prospect_emails(prospect_id);
CREATE INDEX IF NOT EXISTS prospect_emails_sent_idx ON prospect_emails(sent_at);

CREATE TABLE IF NOT EXISTS replies (
    id TEXT PRIMARY KEY,
    prospect_id TEXT REFERENCES prospects(id) ON DELETE SET NULL,
    client_id TEXT REFERENCES clients(id) ON DELETE SET NULL,
    from_address TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    intent TEXT,
    confidence REAL,
    forwarded INTEGER DEFAULT 0,
    forwarded_at TEXT,
    raw_headers TEXT
);

CREATE INDEX IF NOT EXISTS replies_received_idx ON replies(received_at);
CREATE INDEX IF NOT EXISTS replies_intent_idx ON replies(intent);

-- email_opens: each tracking-pixel fire for an outbound send.
CREATE TABLE IF NOT EXISTS email_opens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_email_id TEXT NOT NULL REFERENCES prospect_emails(id) ON DELETE CASCADE,
    opened_at TEXT NOT NULL,
    user_agent TEXT,
    ip_address TEXT,
    is_bot INTEGER DEFAULT 0  -- 1 if request UA matched tracking.bot_user_agents
);

CREATE INDEX IF NOT EXISTS email_opens_pe_idx ON email_opens(prospect_email_id);
CREATE INDEX IF NOT EXISTS email_opens_opened_idx ON email_opens(opened_at);

CREATE TABLE IF NOT EXISTS warmups (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    from_address TEXT NOT NULL,
    client_id TEXT REFERENCES clients(id) ON DELETE SET NULL,
    start_date TEXT NOT NULL,
    target_daily INTEGER DEFAULT 20,
    tool TEXT DEFAULT 'self',
    state TEXT DEFAULT 'warming',
    completed INTEGER DEFAULT 0,
    completed_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warmup_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warmup_id TEXT NOT NULL REFERENCES warmups(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    sent_count INTEGER NOT NULL,
    target_count INTEGER NOT NULL,
    logged_at TEXT NOT NULL,
    UNIQUE(warmup_id, day)
);

CREATE TABLE IF NOT EXISTS hunt_runs (
    id TEXT PRIMARY KEY,
    client_id TEXT REFERENCES clients(id) ON DELETE SET NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source TEXT NOT NULL,
    found_count INTEGER DEFAULT 0,
    inserted_count INTEGER DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS hunt_runs_started_idx ON hunt_runs(started_at);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER DEFAULT 0,
    summary TEXT,
    error TEXT,
    UNIQUE(job, started_at)
);

CREATE INDEX IF NOT EXISTS job_runs_job_idx ON job_runs(job);

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    value TEXT NOT NULL UNIQUE,
    why TEXT,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sender_quotas (
    from_address TEXT NOT NULL,
    day TEXT NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT,
    PRIMARY KEY (from_address, day)
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

-- email_contacts (Task #3): ONE row per email address. When the hunters
-- find a prospect whose email already exists here, they link a new
-- prospects row to the existing contact instead of creating a duplicate.
-- A prospect may also move between clients over time (typical churn) but
-- stays joined to the same email_contact — so a HOT reply forwarded on
-- day 30 still belongs to whichever client owns the prospect that day.
CREATE TABLE IF NOT EXISTS email_contacts (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    business_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    -- provenance: which hunter first saw this email.
    first_source TEXT,
    -- cached decision flags for fast filter at send time.
    unsubscribed INTEGER DEFAULT 0,
    bounced INTEGER DEFAULT 0,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS email_contacts_email_idx ON email_contacts(email);

-- Add contact_id column to existing prospects table (Task #3). Nullable
-- for backward compatibility — old rows that predate the dedup layer
-- simply have NULL until the merge tool rewrites them.

"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _workspace_db_path(workspace_id: str) -> Path:
    d = WORKSPACES_DIR / workspace_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "agency.db"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # These pragmas are important for every connection, not just the
    # very first one that ran executescript — WAL in particular won't
    # be effective on a pre-existing DB file if we don't re-assert it.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def use_workspace(workspace_id: str) -> None:
    """Set which workspace's database this thread's subsequent get_db()
    calls resolve to. Called once at the top of each Flask request (see
    app.py's before_request) or once per scheduler-sweep iteration (see
    scheduler/jobs.py) — never by the business-logic modules themselves."""
    _LOCAL.workspace_id = workspace_id


def current_workspace() -> str | None:
    return getattr(_LOCAL, "workspace_id", None)


def get_db() -> sqlite3.Connection:
    """Connection for the current thread's current workspace. Connections
    are cached per (thread, workspace) so switching workspaces mid-thread
    (e.g. the scheduler sweeping many tenants) doesn't reopen a file it's
    already touched this process."""
    ws = current_workspace()
    if ws is None:
        raise RuntimeError(
            "db.use_workspace(workspace_id) must be called before db.get_db() "
            "— there is no implicit global database anymore."
        )
    conns: dict[str, sqlite3.Connection] = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = {}
        _LOCAL.conns = conns
    conn = conns.get(ws)
    if conn is None:
        conn = _connect(_workspace_db_path(ws))
        conns[ws] = conn
        _ensure_initialized(ws, conn)
    return conn


def _ensure_initialized(workspace_id: str, conn: sqlite3.Connection) -> None:
    """Run schema creation + default settings once per workspace per
    process. Cheap (executescript is idempotent) but memoized anyway since
    it runs on every first touch of a workspace on every thread."""
    if workspace_id in _initialized_workspaces:
        return
    with _init_lock:
        if workspace_id in _initialized_workspaces:
            return
        conn.executescript(SCHEMA)
        _migrate(conn)
        _initialized_workspaces.add(workspace_id)
    ensure_default_settings()


@contextmanager
def transaction():
    """Explicit transaction wrapper."""
    db = get_db()
    db.execute("BEGIN")
    try:
        yield db
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def init_schema() -> None:
    """Create tables + apply column migrations for the CURRENT workspace.
    Safe to call repeatedly — get_db() already does this lazily on first
    touch, so this is mostly useful for scripts that want to be explicit."""
    db = get_db()
    db.executescript(SCHEMA)
    _migrate(db)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive column migrations on tables whose CREATE TABLE IF NOT
    EXISTS already ran on an older schema. New tables are created in SCHEMA.
    Idempotent — re-applying is safe on fresh and upgraded DBs alike."""
    cols_pe = {row["name"] for row in conn.execute("PRAGMA table_info('prospect_emails')").fetchall()}
    if "tracking_id" not in cols_pe:
        conn.execute("ALTER TABLE prospect_emails ADD COLUMN tracking_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS prospect_emails_tracking_idx "
        "ON prospect_emails(tracking_id)"
    )
    cols_eo = {row["name"] for row in conn.execute("PRAGMA table_info('email_opens')").fetchall()}
    if "is_bot" not in cols_eo:
        conn.execute("ALTER TABLE email_opens ADD COLUMN is_bot INTEGER DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS email_opens_is_bot_idx ON email_opens(is_bot)"
    )
    cols_p = {row["name"] for row in conn.execute("PRAGMA table_info('prospects')").fetchall()}
    if "contact_id" not in cols_p:
        conn.execute("ALTER TABLE prospects ADD COLUMN contact_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS prospects_contact_idx ON prospects(contact_id)"
    )


# ── Settings helpers ──────────────────────────────────────────────────────

SENSITIVE_KEYS = ("auth", "webhook_secret")
_SENSITIVE_FIELD_PATHS = {
    "auth": ("password_hash",),
    "webhook_secret": ("value",),
}


def get_setting(key: str, default: Any = None) -> Any:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
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
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), _now()),
    )


# ── Audit helper ──────────────────────────────────────────────────────────

def audit(kind: str, detail: Any = None) -> None:
    try:
        get_db().execute(
            "INSERT INTO audit(kind, detail, created_at) VALUES(?,?,?)",
            (kind, json.dumps(detail) if detail is not None else None, _now()),
        )
    except sqlite3.OperationalError:
        # Audit must never crash the app
        pass


def _safe_json(obj: Any) -> str | None:
    """json.dumps that never raises on non-serialisable values (used by job_runs)."""
    try:
        return json.dumps(obj, default=str)
    except (TypeError, ValueError):
        return None


# ── Blacklist helpers ─────────────────────────────────────────────────────

def blacklist_add(value: str, why: str = "") -> bool:
    db = get_db()
    cur = db.execute("SELECT 1 FROM blacklist WHERE value=?", (value,))
    if cur.fetchone():
        return False
    db.execute(
        "INSERT INTO blacklist(value, why, added_at) VALUES(?,?,?)",
        (value, why, _now()),
    )
    return True


def blacklist_contains(email_or_domain: str) -> bool:
    if not email_or_domain:
        return False
    rows = get_db().execute("SELECT value FROM blacklist").fetchall()
    targets = {r["value"] for r in rows}
    if email_or_domain in targets:
        return True
    if "@" in email_or_domain:
        domain = email_or_domain.split("@", 1)[1]
        return domain in targets
    return False


# ── Sender quota ──────────────────────────────────────────────────────────

def quota_today(from_address: str) -> int:
    today = _now().split("T", 1)[0]
    row = get_db().execute(
        "SELECT sent_count FROM sender_quotas WHERE from_address=? AND day=?",
        (from_address, today),
    ).fetchone()
    return int(row["sent_count"]) if row else 0


def quota_increment(from_address: str) -> int:
    today = _now().split("T", 1)[0]
    db = get_db()
    db.execute(
        "INSERT INTO sender_quotas(from_address, day, sent_count, last_sent_at) "
        "VALUES(?,?,1,?) "
        "ON CONFLICT(from_address, day) DO UPDATE SET "
        "  sent_count=sent_count+1, last_sent_at=excluded.last_sent_at",
        (from_address, today, _now()),
    )
    return quota_today(from_address)


# ── Domain lazy-init: fresh DB returns placeholder rows so the app boots. ─

# ── Open-rate (email tracking pixel) helpers ──────────────────────────────

def lookup_send_by_tracking(tracking_id: str) -> sqlite3.Row | None:
    """Resolve a tracking_id from a pixel request to its prospect_emails row."""
    if not tracking_id:
        return None
    return get_db().execute(
        "SELECT id, prospect_id, client_id FROM prospect_emails WHERE tracking_id=?",
        (tracking_id,),
    ).fetchone()


def _truncate_ip(raw_ip: str) -> str:
    """GDPR/CCPA-friendly IP truncation: keep /24 for IPv4, /48 for IPv6.

    Returns the network address string, or "" if the input is empty or
    unparseable. Industry-standard for open-rate analytics — the truncated
    IP can still distinguish unique recipients at a coarse-grained (e.g.
    office / ASN) level while being too coarse to identify a single person.

    IPv4-mapped-in-IPv6 addresses (e.g. "::ffff:192.168.1.42") are unpacked
    via the .ipv4_mapped attribute so they receive IPv4 /24 truncation
    instead of falling through to IPv6 /48 (which would zero out the whole
    address and yield "::", losing all recipient-resolution value).
    """
    if not raw_ip:
        return ""
    try:
        import ipaddress
        ip = ipaddress.ip_address(raw_ip)
        # Unpack IPv4-mapped-in-IPv6 (RFC 4291 §2.5.5.2) so /24 applies.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        prefix_len = 24 if isinstance(ip, ipaddress.IPv4Address) else 48
        network = ipaddress.ip_network(f"{ip}/{prefix_len}", strict=False)
        return str(network.network_address)
    except (ValueError, TypeError):
        return ""


def record_open(prospect_email_id: str, user_agent: str = "", ip_address: str = "", *, is_bot: bool = False) -> int:
    """Record one tracking-pixel fire.

    `is_bot=True` flags the row as a known scanner/mail-gateway prefetch
    (matched against settings.tracking.bot_user_agents in app.py). Bot
    rows are excluded from open_rate in /api/stats/open-rate but kept on
    disk for analysis. Each fetch still counts separately at this layer;
    SUM(CASE WHEN is_bot=0) is the human-only count.

    IP is truncated to /24 (IPv4) or /48 (IPv6) when
    tracking.import_ip_truncate is True (default). Disable only if you
    have a documented retention policy + privacy notice that allows full IPs.
    """
    truncate = bool(get_setting("tracking", {}).get("import_ip_truncate", True))
    safe_ip = _truncate_ip(ip_address) if truncate else (ip_address or "")[:64]
    cur = get_db().execute(
        "INSERT INTO email_opens(prospect_email_id, opened_at, user_agent, ip_address, is_bot) "
        "VALUES(?,?,?,?,?)",
        (
            prospect_email_id, _now(),
            (user_agent or "")[:512],
            safe_ip[:64],
            1 if is_bot else 0,
        ),
    )
    return int(cur.lastrowid or 0)


def open_count_for_send(prospect_email_id: str) -> int:
    row = get_db().execute(
        "SELECT COUNT(*) AS c FROM email_opens WHERE prospect_email_id=?",
        (prospect_email_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


# ── Domain lazy-init: fresh DB returns placeholder rows so the app boots. ──

DEFAULT_SETTINGS = {
    "agent": {
        "owner_email": "",
        "from_name": "",
        "from_address": "",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_pass": "",
        "smtp_tls": "starttls",
        "imap_host": "",
        "imap_port": 993,
        "imap_user": "",
        "imap_pass": "",
        "imap_ssl": True,
        "morning_brief_to": "",
        "morning_brief_hour": 8,
        "nightly_hunt_hour": 2,
    },
    "apis": {
        "apollo_api_key": "",
        "apollo_use": True,
        "google_places_api_key": "",
        "google_places_use": True,
        "hunter_api_key": "",
        "hunter_use": False,
        "groq_api_key": "",
        "groq_model": "llama-3.3-70b-versatile",
    },
    "tracking": {
        # When enabled, every outbound email embeds a 1x1 tracking GIF at
        # {base_url}/t/o/{tracking_id}.gif so we can compute open rate.
        # base_url MUST be the publicly-reachable https URL behind which
        # this Flask app is reverse-proxied (Caddy terminates TLS).
        "enabled": False,
        "base_url": "",
        # GDPR/CCPA safety: when True (default), every stored IP is
        # truncated to /24 for IPv4 or /48 for IPv6 inside db.record_open so
        # the value can't reasonably identify a single recipient. Disable
        # ONLY if you have a documented privacy notice + retention policy.
        "import_ip_truncate": True,
        # Substrings matched case-insensitively against the /t/o/<id>.gif
        # request UA. Hits stamp is_bot=1 on the row, which the
        # /api/stats/open-rate breakdown excludes from open_rate. These are
        # major corporate mail / URL scanners that pre-fetch embedded
        # URLs in incoming mail — they are NOT real prospect reads.
        "bot_user_agents": [
            "Proofpoint", "Mimecast", "Barracuda", "Symantec",
            "Microsoft Defender", "URLPhishing", "phishtank",
            "Sophos", "IronPort", "FireEye",
        ],
    },
    "warmup": {
        "plan": [
            {"day": 1, "target": 2},
            {"day": 2, "target": 3},
            {"day": 3, "target": 4},
            {"day": 4, "target": 6},
            {"day": 5, "target": 8},
            {"day": 6, "target": 10},
            {"day": 7, "target": 12},
            {"day": 8, "target": 15},
            {"day": 9, "target": 17},
            {"day": 10, "target": 19},
            {"day": 11, "target": 22},
            {"day": 12, "target": 25},
            {"day": 13, "target": 28},
            {"day": 14, "target": 32},
        ],
        "max_daily_after": 40,
    },
    "scheduler": {
        "enabled": False,
        "run_nightly_hunt": True,
        "run_morning_brief": True,
        "run_reply_poller": True,
    },
    "hot_forward": {
        "include_subject": True,
        "include_body": True,
        "include_contact_history": False,
        "include_signature": "— LeadGen AI",
    },
    # auth is intentionally empty on first boot — the operator sets the
    # dashboard password via /wizard step 0 (or POST /api/auth/install with
    # the one-time setup token printed to the orchestrator's stdout on
    # first boot). Inbound webhook URLs (/run-audit and /new-lead) are
    # protected by `webhook_secret`, generated on first boot and visible to
    # the logged-in dashboard via GET /api/auth/webhook/url.
    "auth": {},
    "webhook_secret": {},
    # email_contacts is the global dedup table — same email may only appear
    # once across ALL clients. Hunters insert here; prospects table then
    # links via `contact_id`. See scripts/merge_dup_contacts.py for the
    # one-shot migration over existing data.
    "email_contacts": {},
}


def ensure_default_settings() -> None:
    """Initialise settings keys the app expects if they don't yet exist."""
    db = get_db()
    existing = {row["key"] for row in db.execute("SELECT key FROM settings").fetchall()}
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            set_setting(key, value)
