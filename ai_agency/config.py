"""
Settings loader. Reads JSON files into the `settings` table on first boot and
exposes typed helpers for the rest of the app.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parent / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
NICHES_FILE = CONFIG_DIR / "niches.json"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def write_default_settings_file() -> Path:
    """Write a sample settings.json on first boot so the operator can fill it in."""
    from . import db

    _ensure_dirs()
    if SETTINGS_FILE.exists():
        return SETTINGS_FILE

    sample = {
        "agent": {
            "owner_email": "",
            "from_name": "Your Agency",
            "from_address": "hello@your-new-domain.com",
            "smtp_host": "smtp.your-provider.com",
            "smtp_port": 587,
            "smtp_user": "hello@your-new-domain.com",
            "smtp_pass": "",
            "smtp_tls": "starttls",
            "imap_host": "imap.your-provider.com",
            "imap_port": 993,
            "imap_user": "hello@your-new-domain.com",
            "imap_pass": "",
            "imap_ssl": True,
            "morning_brief_to": "you@gmail.com",
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
        },
    }
    SETTINGS_FILE.write_text(json.dumps(sample, indent=2))
    db.audit("settings.bootstrap_file", {"path": str(SETTINGS_FILE)})
    return SETTINGS_FILE


@lru_cache(maxsize=1)
def load_settings() -> dict[str, Any]:
    """Read all settings for the running process, merged with on-disk file.

    Settings in the SQLite `settings` table win over defaults; values in
    `config/settings.json` initialize the table on first boot.
    """
    from . import db

    # Bootstrap default keys if absent
    db.init_schema()
    db.ensure_default_settings()

    out: dict[str, Any] = {}
    for row in db.get_db().execute("SELECT key, value FROM settings").fetchall():
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            out[row["key"]] = {}
    return out


@lru_cache(maxsize=1)
def load_niches() -> list[dict[str, Any]]:
    if not NICHES_FILE.exists():
        return []
    return json.loads(NICHES_FILE.read_text()).get("niches", [])


def niche_lookup(name: str) -> dict[str, Any] | None:
    for n in load_niches():
        if n["key"].lower() == name.lower():
            return n
    return None

