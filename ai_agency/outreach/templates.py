"""
Email DNA templates.

Each niche has a hooked cold-email opener, a value-prop body, a follow-up
reminder, and a breakup note. The renderer is pure-Python and never touches
the LLM — the generator step additionally rewrites the opener with Llama 3
when available, but the structure is always drawn from this file so we degrade
gracefully when the local LLM is offline.

Avoiding LLM-only output is intentional: it bounds cost, gives the agency
operator a copy-paste fallback, and keeps tone compliance if the LLM drifts.
"""
from __future__ import annotations

import random
import re
from typing import Iterable

GLOBAL_FALLBACK = {
    "tone": "friendly, professional",
    "vocabulary": [],
    "taboo_words": ["synergy", "leverage", "disrupt", "transformative"],
    "subject_templates": [
        "Quick idea for {city} — 5-minute read?",
        "Idea my hunch {businessName} won't hate — 4-min read",
        "Something specific for {city} shops — worth 60 seconds?"
    ],
    "opener_hooks": [
        "{firstName}, quick one — I noticed {businessName}'s workload in {city} looks heavier than this time last quarter. Built a 2-step intake that's saved similar shops ~6 hours/week. Quick look?"
    ],
    "followup_pain": "lead conversion is slow",
    "followup_hook": "How many leads came in last week that never closed?"
}


def _drop_taboos(text: str, taboos: Iterable[str]) -> str:
    for t in taboos:
        if t and t.lower() in text.lower():
            text = re.sub(re.escape(t), "", text, flags=re.IGNORECASE)
    return text


def _pick_random_hook(hooks: list[str]) -> str:
    if not hooks:
        return GLOBAL_FALLBACK["opener_hooks"][0]
    return random.choice(hooks)


def cold_email(niche_dna: dict, *, business_name: str, contact_name: str | None, city: str | None) -> dict[str, str]:
    """Render the cold-email package for a niche."""
    first = (contact_name or "").split()[0] if contact_name else "there"
    template = _pick_random_hook(niche_dna.get("opener_hooks") or GLOBAL_FALLBACK["opener_hooks"])
    body = template.format(
        firstName=first,
        businessName=business_name,
        city=city or "your area",
    )
    subject = _subject(niche_dna, business_name=business_name, city=city or "your area")
    body = _ensure_signature(body, niche_dna)
    taboos = list(niche_dna.get("taboo_words") or []) + list(GLOBAL_FALLBACK["taboo_words"])
    return {
        "subject": _drop_taboos(subject, taboos).strip(),
        "body": _drop_taboos(body, taboos).strip(),
    }


def followup_email(niche_dna: dict, *, business_name: str, contact_name: str | None, city: str | None, step: int) -> dict[str, str]:
    """step 1 = day-3 nudge. step 2 = day-7 breakup."""
    first = (contact_name or "").split()[0] if contact_name else "there"
    if step == 1:
        body = (
            f"{first}, ping — circling back on what I sent {business_name} earlier this week. "
            f"My hunch is your plate's full so the question is genuinely just: is this a problem "
            f"{businessName} {city or ''} wants tackled soon, or no? Honest 'no' is a totally fine answer.\n\n"
            f"— If still a 'maybe,' what's the next 30 days at {city or 'your shop'} looking like?"
        )
        subject = f"Re: {business_name} — quick yes/no?"
    else:
        body = (
            f"{first}, I won't pile on — closing the loop on this. If the timing's off for {businessName} "
            f"right now, no worries at all. Whenever the {niche_dna.get('key', 'category').lower()} workload "
            f"in {city or 'your market'} gets loud again, the playbook I sent is still yours.\n\n"
            f"Wish you a strong quarter."
        )
        subject = f"Closing the loop — {business_name}"
    return {"subject": subject, "body": body}


def value_prop_line(niche_dna: dict, prospect_business: str, city: str | None) -> str:
    """One-line value prop used in campaigns / reports."""
    city_s = city or "your market"
    return (
        f"For a {niche_dna.get('key', 'local service').lower()} shop like {prospect_business} in {city_s}, "
        f"the offer is: {niche_dna.get('followup_hook') or niche_dna.get('followup_pain', 'a steady pipeline of qualified leads')}."
    )


def _subject(niche_dna: dict, *, business_name: str, city: str) -> str:
    """
    Pick a subject line for the niche. Order of preference:

      1. `subject_templates` array on the niche (3 strings; curiosity, value, urgency).
         Templates support {businessName} / {city} placeholders.
      2. `GLOBAL_FALLBACK["subject_templates"]` (generic but still 3 variants).
      3. Hard-coded fallback string (last resort).

    We pick uniformly at random so repeated sends to the same prospect in
    different cadences don't template-fingerprint.
    """
    templates = (
        niche_dna.get("subject_templates")
        or GLOBAL_FALLBACK.get("subject_templates")
        or [f"Quick idea for {city} — 5-min read?"]
    )
    tmpl = random.choice(list(templates))
    # Some particular niches might add {ltv}; safe-format defensively.
    try:
        formatted = tmpl.format(businessName=business_name, city=city)
    except (KeyError, IndexError):
        formatted = tmpl
    return _clean_subject(formatted)


def _clean_subject(s: str) -> str:
    """
    Strip leading/trailing junk that comes from empty placeholders. So
    "{businessName} — quick question?" with an empty business name becomes
    "quick question?" instead of " — quick question?".
    """
    s = s.strip()
    # Strip a single dangling dash / colon / ellipsis at the very start,
    # plus any trailing whitespace it left behind.
    while s and s[0] in ("—", "-", ":", ",", "·", ".", "•"):
        s = s[1:].lstrip(" .,-:·•")
    return s.strip()


def _ensure_signature(body: str, niche_dna: dict) -> str:
    if body.rstrip().endswith(("Reply", "Best", "Cheers", "Thanks")):
        return body
    return body.rstrip() + "\n\n— Paul @ LeadGen AI"


def signature_line() -> str:
    return "\n\n— Paul @ LeadGen AI"
