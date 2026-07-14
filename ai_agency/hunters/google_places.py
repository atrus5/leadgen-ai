"""
Google Places lead hunter.

Pulls local-service-business listings from the official Places API using a
Text Search followed by Place Details for the contact fields. Email is not
exposed by Google, so every result is run through hunters/enrich.py, which
scrapes the business's own website for a real contact address and falls
back to a guessed info@domain address (flagged in `notes`) when nothing is
found.

Endpoint reference:
  POST https://places.googleapis.com/v1/places:searchText
  GET  https://places.googleapis.com/v1/places/{place_id}

We cap to 30 results per client per night and record quota usage.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from typing import Any

import requests

from .. import db
from ..config import load_settings
from . import enrich

LOG = logging.getLogger("hunters.google_places")

DAILY_QUOTA_CAP = 30
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"


def enabled() -> bool:
    s = load_settings()
    apis = s.get("apis", {})
    return bool(apis.get("google_places_use")) and bool(apis.get("google_places_api_key"))


def _headers() -> dict[str, str]:
    key = load_settings()["apis"]["google_places_api_key"]
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        # Field mask: only the fields we actually consume.
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.internationalPhoneNumber,places.websiteUri,"
            "places.primaryType,places.types,places.rating,places.userRatingCount"
        ),
    }


def search(niche: str, city: str) -> list[dict[str, Any]]:
    """Run a Text Search on Places for niche+city; return up to 20 places."""
    if not enabled():
        return []
    body = {
        "textQuery": f"{niche} {city}".strip(),
        "maxResultCount": 20,
    }
    try:
        r = requests.post(TEXT_SEARCH_URL, json=body, headers=_headers(), timeout=30)
    except requests.RequestException as exc:
        db.audit("google_places.error", {"err": str(exc), "niche": niche, "city": city})
        LOG.warning("Places transport error: %s", exc)
        return []
    if r.status_code != 200:
        db.audit("google_places.http_error", {"status": r.status_code, "body": r.text[:200]})
        return []
    data = r.json() or {}
    return data.get("places") or []


def to_prospect_dict(place: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    addr = place.get("formattedAddress") or ""
    city, region = _extract_city_state(addr)
    return {
        "external_id": f"google:{place['id']}",
        "business_name": (place.get("displayName") or {}).get("text"),
        "niche": client["niche"],
        "city": city or client.get("city"),
        "region": region or client.get("region"),
        "country": client.get("country"),
        "website": place.get("websiteUri"),
        "phone": place.get("internationalPhoneNumber"),
        "contact_name": None,
        "contact_email": None,  # not exposed by Places; run_for_client() enriches it below
        "contact_title": None,
        "notes": None,
        "source": "Google Places",
        "score": _score(place),
    }


def _extract_city_state(address: str) -> tuple[str | None, str | None]:
    # Best-effort: look for ", ST 12345" in US-style addresses.
    if not address:
        return None, None
    parts = [p.strip() for p in address.split(",")]
    state = None
    city = None
    if len(parts) >= 3:
        # e.g. ["123 Main St", "Austin", "TX", "78701", "USA"]
        city = parts[-3]
        last = parts[-2]
        state = last.split()[0] if last else None
    return city or None, state


def _score(place: dict[str, Any]) -> int:
    s = 0
    if place.get("websiteUri"):
        s += 2
    rating = place.get("rating") or 0
    reviews = place.get("userRatingCount") or 0
    if reviews >= 10 and rating >= 4.0:
        s += 2
    elif reviews >= 30:
        s += 1
    if place.get("internationalPhoneNumber"):
        s += 1
    ptype = (place.get("primaryType") or "").lower()
    if ptype:
        s += 1
    return min(10, s)


def run_for_client(client: dict[str, Any]) -> dict[str, int]:
    run_id = f"places:{int(time.time()*1000)}"
    db.get_db().execute(
        "INSERT INTO hunt_runs(id, client_id, started_at, source) VALUES(?,?,?,?)",
        (run_id, client["id"], db._now(), "Google Places"),
    )
    found = inserted = 0
    try:
        if not enabled():
            raise RuntimeError("Google Places disabled in settings")
        raw = search(client["niche"], client.get("city") or "")
        # Dedupe against anything Apollo already inserted (same client + same domain).
        existing_domains = _known_domains_for(client["id"])
        for place in raw[:DAILY_QUOTA_CAP]:
            prospect = to_prospect_dict(place, client)
            if prospect.get("website"):
                root = _root_domain(prospect["website"])
                if root and root in existing_domains:
                    found += 1
                    continue
                enrich.enrich_prospect(prospect)
            if prospect.get("business_name") and _upsert_prospect(client, prospect):
                inserted += 1
            found += 1
        db.get_db().execute(
            "UPDATE hunt_runs SET finished_at=?, found_count=?, inserted_count=?, error=? "
            "WHERE id=?",
            (db._now(), found, inserted, None, run_id),
        )
        db.audit("google_places.run", {"client_id": client["id"], "found": found, "inserted": inserted})
    except Exception as exc:
        db.get_db().execute(
            "UPDATE hunt_runs SET finished_at=?, error=? WHERE id=?",
            (db._now(), str(exc), run_id),
        )
        LOG.exception("google_places run failed")
    return {"found": found, "inserted": inserted}


def _root_domain(url: str) -> str | None:
    if not url:
        return None
    s = url.strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s or None


def _known_domains_for(client_id: str) -> set[str]:
    rows = db.get_db().execute(
        "SELECT website FROM prospects WHERE client_id=?",
        (client_id,),
    ).fetchall()
    out = set()
    for r in rows:
        if r["website"]:
            rd = _root_domain(r["website"])
            if rd:
                out.add(rd)
    return out


def _upsert_prospect(client: dict[str, Any], prospect: dict[str, Any]) -> bool:
    conn = db.get_db()
    row = conn.execute(
        "SELECT id FROM prospects WHERE client_id=? AND external_id=?",
        (client["id"], prospect["external_id"]),
    ).fetchone()
    if row:
        return False
    try:
        conn.execute(
            "INSERT INTO prospects(id, client_id, business_name, niche, city, region, country, "
            "website, phone, contact_name, contact_email, contact_title, notes, "
            "source, external_id, score, status, added_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)",
            (
                uuid.uuid4().hex,
                client["id"], prospect["business_name"], prospect["niche"],
                prospect.get("city"), prospect.get("region"), prospect.get("country"),
                prospect.get("website"), prospect.get("phone"),
                prospect.get("contact_name"), prospect.get("contact_email"),
                prospect.get("contact_title"), prospect.get("notes"),
                prospect["source"], prospect["external_id"],
                prospect["score"], db._now(),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False
