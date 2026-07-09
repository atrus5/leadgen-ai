"""
Encrypt-decrypt helper for sensitive values stored in the SQLite settings
table. Backed by the `cryptography` library's Fernet (AES-128-CBC + HMAC
SHA-256) under a key the operator supplies via the LEADGEN_MASTER_KEY
environment variable.

Usage
=====
    from ai_agency import secret_keeper
    sk = secret_keeper.SecretKeeper()
    ciphertext = sk.wrap("supersecret")
    plaintext = sk.unwrap(ciphertext)

The `wrap()` function takes either a plain string OR a dict whose leave
values at the named dotted-paths should be encrypted (e.g. {password_hash:
"..."} for the auth key). When called with no field paths, the entire
dict is JSON-serialised, then the resulting string is encrypted.

When LEADGEN_MASTER_KEY is unset, wrap/unwrap are no-ops and values pass
through verbatim \u2014 useful for development and the unit-test suite where
installing a key is overkill. The orchestrator's bootstrap.sh refuses to
start without the env var in production (warns loudly otherwise).
"""
from __future__ import annotations

import base64
import functools
import hashlib
import logging
import os
from typing import Any, Iterable

LOG = logging.getLogger("ai_agency.secret_keeper")

_KEY_ENV = "LEADGEN_MASTER_KEY"


def _derive_key(raw: str) -> bytes:
    """Derive a 32-byte Fernet-compatible key from the operator-supplied
    passphrase. SHA-256 + base64-url-encode the result. Deterministic so
    the same passphrase + the same DB yields the same ciphertext after a
    restart (the test rig can therefore round-trip values without random
    salt per record)."""
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretKeeper:
    """Stateless wrapper around Fernet. Re-derives the underlying Fernet
    on every call so rotating LEADGEN_MASTER_KEY takes effect without a
    process restart."""

    def __init__(self) -> None:
        self._raw = os.environ.get(_KEY_ENV) or ""
        self._fernet = None
        if self._raw:
            try:
                from cryptography.fernet import Fernet
                self._fernet = Fernet(_derive_key(self._raw))
            except ImportError:
                LOG.warning(
                    "cryptography package missing \u2014 LEADDEN_MASTER_KEY env "
                    "var set, but install cryptography before relying on "
                    "encryption at rest."
                )
        else:
            LOG.info(
                "LEADGEN_MASTER_KEY not set \u2014 secret-keeping at-rest is "
                "DISABLED (dev mode). Set the env var in production."
            )

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def wrap(self, value: Any, *, field_paths: Iterable[str] = ()) -> Any:
        """Encrypt `value` if a key is configured. Returns ciphertext
        (or the plaintext if no key); the caller shouldn't care because
        `unwrap` returns the plaintext either way.
        """
        if not self._fernet:
            return value
        if isinstance(value, str):
            return {"__enc__": self._encode(value)}
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                dotted = k  # only top-level paths accepted today
                if dotted in set(field_paths) and isinstance(v, str):
                    out[k] = self._encode(v)
                else:
                    out[k] = v
            return out
        return value

    def unwrap(self, value: Any) -> Any:
        if not self._fernet:
            return value
        if isinstance(value, dict) and "__enc__" in value:
            return self._decode(value["__enc__"])
        if isinstance(value, dict):
            return {k: (self._decode(v) if isinstance(v, str) and v.startswith("enc:") else v)
                    for k, v in value.items()}
        return value

    def _encode(self, plain: str) -> str:
        return "enc:" + self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def _decode(self, blob: str) -> str:
        if blob.startswith("enc:"):
            blob = blob[4:]
        return self._fernet.decrypt(blob.encode("ascii")).decode("utf-8")


@functools.lru_cache(maxsize=1)
def get() -> SecretKeeper:
    return SecretKeeper()


# Module-level convenience wrappers (used by db.get_setting/set_setting).
def wrap(value: Any, *, field_paths: Iterable[str] = ()) -> Any:
    return get().wrap(value, field_paths=field_paths)


def unwrap(value: Any) -> Any:
    return get().unwrap(value)
