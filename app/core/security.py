"""Password hashing (argon2id) and opaque session tokens."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()
# Verified against when an email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = _hasher.hash("agrictec-timing-equalizer")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and bool(password_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """Only this digest is stored, so a leaked database holds no usable session."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign(value: str) -> str:
    """value.signature, for the long-lived guest device cookie."""
    key = get_settings().secret_key.encode("utf-8")
    mac = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{mac}"


def unsign(signed: str | None) -> str | None:
    if not signed or "." not in signed:
        return None
    value = signed.rsplit(".", 1)[0]
    return value if hmac.compare_digest(sign(value), signed) else None


def fingerprint(*parts: str) -> str:
    """A one-way identifier of configuration values (never stored in plain text)."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
