"""Encryption for the secrets that have to live in the database.

Two things must be stored and later used: each tenant's LinkedIn client secret,
and their OAuth tokens. Both are bearer credentials for someone else's
identity, so a leaked database dump must not be enough to post as them.

Fernet (AES-128-CBC with an HMAC) from `cryptography`, keyed by
`LNP_ENCRYPTION_KEY`. The key never goes in the database — losing it means
every tenant re-authorises, which is recoverable; sharing it with the database
means the encryption bought nothing.

Written as a SQLAlchemy type rather than encrypt/decrypt calls at the call
sites, because a call site is a place somebody can forget.
"""

from __future__ import annotations

import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text, TypeDecorator

ENV_KEY = "LNP_ENCRYPTION_KEY"


class CryptoError(Exception):
    pass


def generate_key() -> str:
    """A new key, for the operator to put in the environment. Printed once."""
    return Fernet.generate_key().decode("ascii")


def _fernet(key: Optional[str] = None) -> Fernet:
    raw = key or os.environ.get(ENV_KEY, "")
    if not raw:
        raise CryptoError(
            f"{ENV_KEY} is not set. Generate one with "
            f"`python -c 'from lnp.db.crypto import generate_key; print(generate_key())'` "
            f"and set it in the environment. Without it, stored credentials "
            f"cannot be read."
        )
    try:
        return Fernet(raw.encode("ascii") if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"{ENV_KEY} is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str, key: Optional[str] = None) -> str:
    if plaintext is None or plaintext == "":
        return ""
    return _fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str, key: Optional[str] = None) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError(
            "stored credential could not be decrypted. This normally means "
            f"{ENV_KEY} has changed since it was written; the affected tenants "
            "must reconnect LinkedIn."
        ) from exc


class Encrypted(TypeDecorator):
    """A Text column whose value is encrypted on the way in.

    Ciphertext is not searchable, which is the point: nothing should ever be
    querying on a token value.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value or "")

    def process_result_value(self, value, dialect):
        return decrypt(value or "")
