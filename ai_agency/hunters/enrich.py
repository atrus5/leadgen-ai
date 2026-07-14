"""
Free email enrichment for prospects that have a website but no email — this
is mainly for Google Places leads, since Google's API never exposes contact
email at all.

Two-step, no paid API required:
  1. Fetch the site's homepage (and, if that fails to yield anything, a
     /contact page) and look for a mailto: link or a visible email address.
     This is a REAL address pulled from the business's own site, safe to
     treat as confirmed.
  2. If nothing is found, fall back to guessing the most common local-
     service-business inbox: info@domain. This is a GUESS, not a verified
     address — callers should flag it as lower-confidence (a wrong guess
     causes a bounce, which is exactly what domain warmup exists to avoid).

Never raises — network/parse failures just fall through to the guess.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import requests

LOG = logging.getLogger("hunters.enrich")

TIMEOUT = 8
MAX_HTML_BYTES = 300_000
GUESS_INBOX = "info"

MAILTO_RE = re.compile(r"mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", re.IGNORECASE)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# Addresses that show up in trackers/CDNs/theme placeholders — never a real contact.
JUNK_SUBSTRINGS = ("example.com", "sentry.io", "wixpress.com", "godaddy.com", "yourdomain", "@2x", "schema.org")


def _root_domain(url: str) -> str | None:
    if not url:
        return None
    s = url.strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.split("/", 1)[0]
    return s[4:] if s.startswith("www.") else s or None


def _fetch(url: str) -> str | None:
    try:
        r = requests.get(
            url, timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadGenAI/1.0; +email-lookup)"},
        )
        if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
            return r.text[:MAX_HTML_BYTES]
    except requests.RequestException as exc:
        LOG.debug("enrich fetch failed for %s: %s", url, exc)
    return None


def _extract_email(html: str, domain: str) -> str | None:
    candidates: list[str] = []
    for pattern in (MAILTO_RE, EMAIL_RE):
        candidates.extend(m.lower() for m in pattern.findall(html))
    clean = [e for e in candidates if not any(j in e for j in JUNK_SUBSTRINGS)]
    if not clean:
        return None
    # Prefer an address on the business's own domain over a stray third-party
    # one that's just present on the page (support widgets, share buttons).
    same_domain = [e for e in clean if domain in e]
    return (same_domain or clean)[0]


def find_email(website: str) -> tuple[str | None, bool]:
    """Returns (email, is_guess). Never raises. is_guess=True means the
    address was pattern-guessed, not confirmed on the business's own site."""
    domain = _root_domain(website)
    if not domain:
        return None, False
    base = website if website.startswith("http") else f"https://{website}"
    base = base.rstrip("/") + "/"

    html = _fetch(base)
    if html is not None:
        found = _extract_email(html, domain)
        if found:
            return found, False
        contact_html = _fetch(urljoin(base, "contact"))
        if contact_html:
            found = _extract_email(contact_html, domain)
            if found:
                return found, False

    return f"{GUESS_INBOX}@{domain}", True


def enrich_prospect(prospect: dict[str, Any]) -> None:
    """Mutates `prospect` in place: fills contact_email via find_email() when
    the hunter didn't return a verified one. Guessed (unverified) addresses
    are flagged in `notes` and score a smaller bump than a confirmed address
    scraped straight off the business's own site. No-op if an email is
    already present or there's no website to check."""
    if prospect.get("contact_email") or not prospect.get("website"):
        return
    email, is_guess = find_email(prospect["website"])
    if not email:
        return
    prospect["contact_email"] = email
    if is_guess:
        prospect["notes"] = "Email guessed from domain pattern — unverified, review before relying on it."
        prospect["score"] = min(10, prospect["score"] + 1)
    else:
        prospect["score"] = min(10, prospect["score"] + 2)
