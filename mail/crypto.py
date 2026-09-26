"""Encryption at rest for mailbox credentials.

A mailbox row holds a password or an OAuth refresh token -- full access to someone's mail. Both
are stored Fernet-encrypted with the key(s) in the file at ``secrets.key_path``, so a database
dump alone is not enough. The file holds one key per line: the first encrypts, every one
decrypts (:class:`~cryptography.fernet.MultiFernet`), so a key is rotated by putting a new one on
top, running ``manage.py rotate_secrets`` and then dropping the old one.
"""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


@lru_cache(maxsize=4)
def _fernet(path: str) -> MultiFernet:
    keys = [line.strip() for line in Path(path).read_text().splitlines() if line.strip() and not line.startswith("#")]
    if not keys:
        raise ValueError(f"No Fernet key in {path}.")
    return MultiFernet([Fernet(key) for key in keys])


def _current() -> MultiFernet:
    return _fernet(settings.KUVERT_SECRETS["key_path"])


def encrypt(value: str) -> str:
    """``value`` encrypted with the first key."""
    return _current().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """``value`` decrypted with whichever key encrypted it."""
    return _current().decrypt(value.encode()).decode()


def rotate(value: str) -> str:
    """``value`` re-encrypted with the first key."""
    return _current().rotate(value.encode()).decode()


def encrypt_optional(value: str | None) -> str | None:
    """:func:`encrypt`, passing None (and an empty value) through as None."""
    return encrypt(value) if value else None


def decrypt_optional(value: str | None) -> str | None:
    """:func:`decrypt`, passing None through."""
    return decrypt(value) if value else None
