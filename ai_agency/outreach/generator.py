"""
Email generator. Composes the niche-DNA template and (optionally) personalises
the opener using Groq's hosted chat-completions API. The LLM step is
optional — when Groq is unreachable or unconfigured, the renderer falls back
to the templated body, which is intentionally already decked out with the
niche's hooks.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import config, llm
from . import templates

LOG = logging.getLogger("outreach.generator")

LLAMA_TIMEOUT_SECONDS = 30


def _llama_available() -> bool:
    return llm.available()


def _rewrite_with_llama(niche: str, base_body: str, *, business: str, contact: str | None, city: str | None, ltv: str) -> str | None:
    """Use Groq to refine the templated body. Never raises."""
    try:
        system = (
            "You are a cold-email copywriter for local-service businesses. "
            "You produce 4-6 sentence cold emails. Tone: plain-spoken, "
            "never salesy, never pressuring. Hard rule: do not invent facts "
            "about the recipient; only rephrase what you were given. "
            "Hard rule: under 90 words total."
        )
        user = (
            f"Niche: {niche}\n"
            f"Recipient business: {business}\n"
            f"Recipient contact (may be empty): {contact or 'unknown'}\n"
            f"City: {city or 'unspecified'}\n"
            f"Customer-lifetime-value estimate: {ltv}\n\n"
            "Rewrite the email body to read like it was written by a real person "
            "for a real local owner-operator. Keep the core hook. Output ONLY the body. "
            f"Body to rewrite:\n\n{base_body}"
        )
        return llm.chat(system, user, temperature=0.4, max_tokens=220, timeout=LLAMA_TIMEOUT_SECONDS)
    except Exception as exc:
        LOG.warning("groq rewrite skipped: %s", exc)
        return None


def generate_cold_email(*, niche: str, business: str, contact: str | None, city: str | None) -> dict[str, str]:
    niche_dna = config.niche_lookup(niche) or {}
    templated = templates.cold_email(
        niche_dna,
        business_name=business,
        contact_name=contact,
        city=city,
    )
    body = templated["body"]
    subject = templated["subject"]
    if _llama_available():
        rewritten = _rewrite_with_llama(
            niche, body, business=business, contact=contact, city=city,
            ltv=niche_dna.get("lifetime_value", ""),
        )
        if rewritten:
            body = rewritten
            if not body.rstrip().endswith("Reply") and not body.rstrip().endswith("LeadGen AI"):
                body = body.rstrip() + templates.signature_line()
    return {"subject": subject, "body": body}


def generate_followup(*, niche: str, business: str, contact: str | None, city: str | None, step: int) -> dict[str, str]:
    niche_dna = config.niche_lookup(niche) or {}
    return templates.followup_email(
        niche_dna, business_name=business, contact_name=contact, city=city, step=step,
    )


def value_prop(niche: str, business: str, city: str | None) -> str:
    niche_dna = config.niche_lookup(niche) or {}
    return templates.value_prop_line(niche_dna, business, city)
