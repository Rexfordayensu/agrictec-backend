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
# Across every address: someone guessing one account's password from many addresses (each
# staying under the per-address lock) is stopped here. Thirty wrong passwords in a row lock the
# account for 15 minutes (the owner can still reset their password).
ACCOUNT_MAX_FAILURES = 30
ACCOUNT_LOCKOUT_MINUTES = 15


def lockout_key(email: str, client: str, scope: str = "admin") -> str:
    """The lockout row for one email from one address (hashed: both are personal data). Admin
    keys keep their original form; farmers have their own, so the two never share a count."""
    address = email.strip().lower()
    raw = f"{address}|{client}" if scope == "admin" else f"{scope}|{address}|{client}"
    return hashlib.sha256(raw.encode()).hexdigest()


def account_key(email: str, scope: str) -> str:
    """The account-wide row for one email (every address together), per kind of account."""
    return hashlib.sha256(f"account|{scope}|{email.strip().lower()}".encode()).hexdigest()


def lock_status(db: DB, key: str, now: datetime, limit: int = MAX_LOGIN_ATTEMPTS) -> LockStatus:
    row = db.get(LoginLockout, key)
    if row and row.locked_until and row.locked_until > now:
        return LockStatus(locked=True, until=row.locked_until, attempts_left=0)
    # An expired lock starts a fresh set of attempts.
    failures = 0 if not row or row.locked_until else row.failures
    return LockStatus(locked=False, attempts_left=max(0, limit - failures))


def fail_attempt(
    db: DB, key: str, now: datetime, limit: int = MAX_LOGIN_ATTEMPTS, minutes: int = LOCKOUT_MINUTES
) -> LockStatus:
    """Counts one wrong password; the `limit`th in a row locks the row for `minutes`."""
    current = lock_status(db, key, now, limit)
    if current.locked:
        return current
    failures = limit - current.attempts_left + 1
    row = db.get(LoginLockout, key) or LoginLockout(key=key)
    row.failures = failures
    row.locked_until = now + timedelta(minutes=minutes) if failures >= limit else None
    db.merge(row)
    if row.locked_until:
        return LockStatus(locked=True, until=row.locked_until, attempts_left=0)
    return LockStatus(locked=False, attempts_left=limit - failures)


def check(db: DB, email: str, client: str, scope: str, now: datetime) -> tuple[str, str]:
    """Refuses a sign-in while this email is locked from this address or account-wide (423,
    checked before the password, so a right guess during a lock gets nothing). The two keys."""
    pair = lockout_key(email, client, scope=scope)
    whole = account_key(email, scope)
    for key, limit in ((pair, MAX_LOGIN_ATTEMPTS), (whole, ACCOUNT_MAX_FAILURES)):
        lock = lock_status(db, key, now, limit)
        if lock.locked and lock.until:
            raise locked_error(lock.until, now)
    return pair, whole


def failed(db: DB, keys: tuple[str, str], now: datetime) -> LockStatus:
    """Counts a wrong password against both keys; the stricter lock wins."""
    pair, whole = keys
    first = fail_attempt(db, pair, now)
    account = fail_attempt(db, whole, now, ACCOUNT_MAX_FAILURES, ACCOUNT_LOCKOUT_MINUTES)
    return account if account.locked else first


def succeeded(db: DB, keys: tuple[str, str]) -> None:
    for key in keys:
        clear(db, key)


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
