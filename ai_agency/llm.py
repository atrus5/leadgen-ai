"""
Thin wrapper around Groq's chat-completions API (OpenAI-compatible REST).

Replaces the old local-Ollama integration: there is no daemon to keep
running, just an API key. Every caller treats the LLM step as optional —
`chat()` raises on any failure and callers fall back to their deterministic
heuristic (keyword classifier, plain template) exactly as they did before.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from . import config

LOG = logging.getLogger("llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


def _apis() -> dict[str, Any]:
    return config.load_settings().get("apis", {})


def api_key() -> str:
    return (_apis().get("groq_api_key") or "").strip()


def model() -> str:
    return (_apis().get("groq_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def available() -> bool:
    return bool(api_key())


def chat(system: str, user: str, *, temperature: float = 0.4, max_tokens: int = 400, timeout: int = 20) -> str:
    """Call Groq chat completions. Raises (requests.HTTPError, RuntimeError, ...)
    on any failure — callers decide the fallback, matching the old Ollama contract."""
    key = api_key()
    if not key:
        raise RuntimeError("apis.groq_api_key not configured")
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model(),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise RuntimeError(f"groq replied with empty content from model '{model()}'")
    return text
