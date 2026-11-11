"""The sign-in lockout, for farmers and admins alike.

Failures are counted per email AND visitor address: five wrong passwords in a row lock that pair
for five minutes. Someone guessing at another person's email only locks themselves out, never
the owner signing in from their own connection. A wrong password says only "Incorrect email or
password" (no countdown of attempts); the lock says when signing in works again."""

import hashlib
import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session as DB

from app.core.errors import Locked
from app.models import LoginLockout
from app.schemas.admin import LockStatus

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 5


def lockout_key(email: str, client: str, scope: str = "admin") -> str:
    """The lockout row for one email from one address (hashed: both are personal data). Admin
    keys keep their original form; farmers have their own, so the two never share a count."""
    address = email.strip().lower()
    raw = f"{address}|{client}" if scope == "admin" else f"{scope}|{address}|{client}"
    return hashlib.sha256(raw.encode()).hexdigest()


def lock_status(db: DB, key: str, now: datetime) -> LockStatus:
    row = db.get(LoginLockout, key)
    if row and row.locked_until and row.locked_until > now:
        return LockStatus(locked=True, until=row.locked_until, attempts_left=0)
    # An expired lock starts a fresh set of attempts.
    failures = 0 if not row or row.locked_until else row.failures
    return LockStatus(locked=False, attempts_left=max(0, MAX_LOGIN_ATTEMPTS - failures))


def fail_attempt(db: DB, key: str, now: datetime) -> LockStatus:
    """Counts one wrong password; the fifth in a row locks the pair."""
    current = lock_status(db, key, now)
    if current.locked:
        return current
    failures = MAX_LOGIN_ATTEMPTS - current.attempts_left + 1
    row = db.get(LoginLockout, key) or LoginLockout(key=key)
    row.failures = failures
    row.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES) if failures >= MAX_LOGIN_ATTEMPTS else None
    db.merge(row)
    if row.locked_until:
        return LockStatus(locked=True, until=row.locked_until, attempts_left=0)
    return LockStatus(locked=False, attempts_left=MAX_LOGIN_ATTEMPTS - failures)


def clear(db: DB, key: str) -> None:
    """A successful sign-in forgets the failures."""
    row = db.get(LoginLockout, key)
    if row:
        db.delete(row)


def sign_in_again_at(until: datetime) -> datetime:
    """The lock's end rounded up to the minute, so the time shown is never too early."""
    whole = until.replace(second=0, microsecond=0)
    return whole if whole == until else whole + timedelta(minutes=1)


def locked_message(until: datetime, now: datetime) -> str:
    """"Too many attempts. You can sign in again at 14:35 GMT (in 5 minutes)." Ghana time is GMT."""
    minutes = max(1, math.ceil((until - now).total_seconds() / 60))
    at = sign_in_again_at(until)
    return (
        f"Too many attempts. You can sign in again at {at:%H:%M} GMT "
        f"(in {minutes} minute{'' if minutes == 1 else 's'})."
    )


def locked_error(until: datetime, now: datetime) -> Locked:
    """423 with `until`, so the sign-in page can show the time and count down to it."""
    return Locked(locked_message(until, now), code="locked", extra={"until": until.isoformat()})
