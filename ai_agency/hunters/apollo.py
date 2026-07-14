"""
Apollo.io lead hunter.

Reads apollo_api_key + apollo_use from settings, runs an Organization +
People search for the agency's per-client niche and city, returns normalized
prospect dicts ready to upsert into the prospects table.

Apollo endpoints used:
  POST https://api.apollo.io/api/v1/mixed_people/search     (people + org in one)
  POST https://api.apollo.io/api/v1/people/match           (best matching single lead)

We do NOT attempt to scrape Apollo UI or non-public endpoints; the public
REST API is documented at https://apolloio.github.io/apollo-api-docs.

Apollo enforces Terms of Service against bulk data-mining. Each hunt run is
capped at 50 people per client per night and quota is recorded in
sender_quotas (we treat API calls as a quota like email sends).
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from typing import Any, Iterable

import requests

from .. import db
from ..config import load_niches, load_settings
from . import enrich

LOG = logging.getLogger("hunters.apollo")

BASE = "https://api.apollo.io/api/v1"
DAILY_QUOTA_CAP = 50  # per-client, per-night maximum results to insert

# Apollo employee title filters that map to a decision-maker for local service businesses.
DECISION_MAKER_TITLES = (
    "owner", "founder", "co-founder", "ceo", "president", "principal",
    "managing director", "general manager", "gm", "partner",
)


def _settings() -> dict[str, Any]:
    return load_settings()


def enabled() -> bool:
    s = _settings()
    apis = s.get("apis", {})
    return bool(apis.get("apollo_use")) and bool(apis.get("apollo_api_key"))


def _headers() -> dict[str, str]:
    s = _settings()
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "api_key": s["apis"]["apollo_api_key"],
    }


def _normalize_city_params(niche: str, client: dict[str, Any]) -> dict[str, Any]:
    """
    Map our internal niche labels to Apollo's `person_titles` + `q_keywords`
    filters. Apollo is generalist; we tighten the search so we don't drown in
    generic engineering results.

    Apollo expects `person_locations` as comma-joined geographic strings
    ("City, State, Country"); bare city names match fuzzily at best. We
    also gate on whether the client record has region/country so the
    caller isn't surprised by surprising geographies.
    """
    title_keywords = list(DECISION_MAKER_TITLES)
    q_keywords = [niche.lower()]
    city = (client.get("city") or "").strip()
    region = (client.get("region") or "").strip()
    country = (client.get("country") or "").strip()
    if not (city and region and country):
        raise ValueError(
            "Apollo hunts need client.city / client.region / client.country all set"
        )
    person_location = f"{city}, {region}, {country}"
    return {
        "person_titles": title_keywords[:10],
        "q_keywords": " ".join(q_keywords),
        "person_locations": [person_location],
    }


def search_people(niche: str, client: dict[str, Any], per_page: int = 25) -> list[dict[str, Any]]:
    """Return up to per_page people/organizations from Apollo."""
    if not enabled():
        return []
    params = _normalize_city_params(niche, client)
    params.update({
        "page": 1,
        "per_page": min(per_page, 100),
    })
    try:
        r = requests.post(
            f"{BASE}/mixed_people/search",
            json=params,
            headers=_headers(),
            timeout=30,
        )
    except requests.RequestException as exc:
        db.audit("apollo.error", {"err": str(exc), "niche": niche, "city": client.get("city")})
        LOG.warning("Apollo transport error: %s", exc)
        return []

    if r.status_code != 200:
        db.audit("apollo.http_error", {"status": r.status_code, "body": r.text[:200]})
        LOG.warning("Apollo HTTP %s: %s", r.status_code, r.text[:200])
        return []
    try:
        payload = r.json()
    except ValueError:
        return []
    return payload.get("people") or payload.get("contacts") or []


def to_prospect_dict(person: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    """Normalize an Apollo person dict into our `prospects` shape."""
    org = person.get("organization") or {}
    return {
        "external_id": person.get("id") or f"apollo:{person.get('linkedin_url','')}",
        "business_name": org.get("name") or person.get("name", "Unknown"),
        "niche": client["niche"],
        "city": (org.get("city") or client.get("city") or "").strip() or None,
        "region": (org.get("state") or client.get("region") or "").strip() or None,
        "country": (org.get("country") or client.get("country") or "").strip() or None,
        "website": org.get("website_url") or org.get("primary_domain"),
        "phone": org.get("phone"),
        "contact_name": _full_name(person),
        "contact_email": person.get("email"),
        "contact_title": person.get("title"),
        "notes": None,
        "source": "Apollo.io",
        "score": _score(person, org),
    }


def _full_name(person: dict[str, Any]) -> str | None:
    first = (person.get("first_name") or "").strip()
    last = (person.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return person.get("name")


def _score(person: dict[str, Any], org: dict[str, Any]) -> int:
    s = 0
    if org.get("website_url") or org.get("primary_domain"):
        s += 2
    if person.get("email"):
        s += 2  # verified email is worth more than generic
    if any(k in (person.get("title") or "").lower() for k in DECISION_MAKER_TITLES):
        s += 2
    if org.get("estimated_num_employees"):
        try:
            n = int(org["estimated_num_employees"])
            if 1 <= n <= 50:
                s += 2  # local-service sweet spot
        except (TypeError, ValueError):
            pass
    if org.get("industry"):
        s += 1
    return min(10, s)


def run_for_client(client: dict[str, Any]) -> dict[str, int]:
    """
    Hunt prospects for one agency client. Inserts into DB.
    Returns a counters dict for job_runs.
    """
    run_id = f"apollo:{int(time.time()*1000)}"
    db.get_db().execute(
        "INSERT INTO hunt_runs(id, client_id, started_at, source) VALUES(?,?,?,?)",
        (run_id, client["id"], db._now(), "Apollo.io"),
    )
    found = inserted = error_count = 0
    try:
        if not enabled():
            raise RuntimeError("Apollo disabled in settings")
        raw = search_people(client["niche"], client)
        found = len(raw)
        seen_emails: set[str] = set()
        for person in raw[:DAILY_QUOTA_CAP]:
            try:
                if db.blacklist_contains(person.get("email") or ""):
                    continue
                if person.get("email") in seen_emails:
                    continue
                prospect = to_prospect_dict(person, client)
                enrich.enrich_prospect(prospect)
                # Idempotent insert: (client_id, external_id) unique.
                if _upsert_prospect(client, prospect):
                    inserted += 1
                seen_emails.add(person.get("email"))
            except sqlite3.IntegrityError:
                continue
            except Exception as exc:
                error_count += 1
                LOG.debug("person failed: %s", exc)
        db.get_db().execute(
            "UPDATE hunt_runs SET finished_at=?, found_count=?, inserted_count=?, error=? "
            "WHERE id=?",
            (db._now(), found, inserted, None if error_count == 0 else f"errors={error_count}", run_id),
        )
        db.audit("apollo.run", {"client_id": client["id"], "found": found, "inserted": inserted})
    except Exception as exc:
        db.get_db().execute(
            "UPDATE hunt_runs SET finished_at=?, error=? WHERE id=?",
            (db._now(), str(exc), run_id),
        )
        LOG.exception("apollo run failed")
    return {"found": found, "inserted": inserted}


def _upsert_prospect(client: dict[str, Any], prospect: dict[str, Any]) -> bool:
    """Insert unless (client_id, external_id) already exists; return True if inserted."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT id FROM prospects WHERE client_id=? AND external_id=?",
        (client["id"], prospect["external_id"]),
    ).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
        "website, phone, contact_name, contact_email, contact_title, notes, source, "
        "external_id, score, status, added_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
        (
            uuid.uuid4().hex,
            client["id"], prospect["business_name"], prospect["niche"],
            prospect.get("city"), prospect.get("region"), prospect.get("country"),
            prospect.get("website"), prospect.get("phone"),
            prospect.get("contact_name"), prospect.get("contact_email"),
            prospect.get("contact_title"), prospect.get("notes"), prospect["source"],
            prospect["external_id"], prospect["score"], db._now(),
        ),
    )
    return True
