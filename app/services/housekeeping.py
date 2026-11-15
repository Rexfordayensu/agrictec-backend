"""Clearing out sign-in data that has served its purpose, so it isn't kept for ever: ended or
expired sessions, used or expired view-as-farmer tokens, password-reset and phone codes, and
lockouts that have run out. Runs with the background jobs (app/services/report_schedules.py),
at most every few hours per worker; each step only deletes rows past their use, so running it
twice (several workers) does no harm."""

import time
from datetime import timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session as DB

from app.domain.clock import now_utc
from app.models import AuthSession, ImpersonationToken, LoginLockout, PasswordReset, PhoneCode

# Kept a while after they end, for support questions ("was I signed in yesterday?").
SESSION_DAYS = 30
# Long enough for the daily limits that count recent codes (5 a day).
CODE_DAYS = 7
EVERY_SECONDS = 6 * 3600

_last_run = [0.0]


def purge_expired(db: DB) -> dict[str, int]:
    """Deletes what has served its purpose; how many rows of each."""
    now = now_utc()
    old_sessions = now - timedelta(days=SESSION_DAYS)
    old_codes = now - timedelta(days=CODE_DAYS)
    counts = {
        "sessions": db.execute(delete(AuthSession).where(or_(
            AuthSession.ended_at < old_sessions, AuthSession.expires_at < old_sessions,
        ))).rowcount,
        "impersonation_tokens": db.execute(delete(ImpersonationToken).where(
            ImpersonationToken.expires_at < now - timedelta(days=1),
        )).rowcount,
        "password_resets": db.execute(delete(PasswordReset).where(PasswordReset.expires_at < old_codes)).rowcount,
        "phone_codes": db.execute(delete(PhoneCode).where(PhoneCode.expires_at < old_codes)).rowcount,
        "lockouts": db.execute(delete(LoginLockout).where(
            LoginLockout.locked_until.is_not(None), LoginLockout.locked_until < now - timedelta(days=1),
        )).rowcount,
    }
    db.commit()
    return counts


def run_if_due(db: DB) -> dict[str, int] | None:
    """purge_expired, at most every EVERY_SECONDS in this worker."""
    if time.monotonic() - _last_run[0] < EVERY_SECONDS and _last_run[0]:
        return None
    _last_run[0] = time.monotonic()
    return purge_expired(db)
