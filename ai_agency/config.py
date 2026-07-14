"""
Settings loader. Exposes typed, per-workspace helpers for the rest of the app.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parent / "config"
NICHES_FILE = CONFIG_DIR / "niches.json"


@lru_cache(maxsize=64)
def _load_settings_cached(workspace_id: str) -> dict[str, Any]:
    from . import db

    # db.get_db() lazily runs init_schema()+ensure_default_settings() the
    # first time this workspace is touched, so nothing to bootstrap here.
    out: dict[str, Any] = {}
    for row in db.get_db().execute("SELECT key, value FROM settings").fetchall():
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            out[row["key"]] = {}
    return out


def load_settings() -> dict[str, Any]:
    """Read all settings for the CURRENT workspace — db.use_workspace(id)
    must already have been called (see app.py's before_request and
    scheduler/jobs.py's sweep functions). Cached per-workspace until
    invalidate_settings() is called."""
    from . import db

    ws = db.current_workspace()
    if ws is None:
        raise RuntimeError(
            "db.use_workspace(workspace_id) must be called before config.load_settings()"
        )
    return _load_settings_cached(ws)


def invalidate_settings() -> None:
    """Clear the settings cache for every workspace. A full clear (rather
    than precise per-workspace eviction) is fine at the "low tens of
    tenants" scale this app targets — the next load_settings() call for any
    workspace just re-reads its row."""
    _load_settings_cached.cache_clear()


@lru_cache(maxsize=1)
def load_niches() -> list[dict[str, Any]]:
    if not NICHES_FILE.exists():
        return []
    return json.loads(NICHES_FILE.read_text(encoding="utf-8")).get("niches", [])


def niche_lookup(name: str) -> dict[str, Any] | None:
    for n in load_niches():
        if n["key"].lower() == name.lower():
            return n
    return None


def tracking_base_url() -> str:
    return (load_settings().get("tracking") or {}).get("base_url") or ""

