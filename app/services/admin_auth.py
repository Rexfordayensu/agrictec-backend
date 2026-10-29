"""Admin sign-in, sessions and the lockout (services/admin/admin-auth.service.ts).

Farmer and admin sessions are separate: neither one ever signs the other out."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.errors import SESSION_ENDED, AppError, Forbidden, Locked, Unauthorized
from app.core.security import hash_password, verify_password
from app.core.sessions import create_session, end_session
from app.domain.clock import now_utc
from app.domain.passwords import password_problem
from app.domain.permissions import permissions_of
from app.models import Admin, AuthSession, LoginLockout
from app.schemas.admin import AdminSession, LockStatus
from app.services.mappers import admin_out

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 5


def idle_timeout() -> timedelta:
    return timedelta(minutes=max(1, get_settings().admin_idle_minutes))


def is_idle(record: AuthSession, now: datetime) -> bool:
    return now - record.last_activity_at >= idle_timeout()


def session_out(admin: Admin, record: AuthSession) -> AdminSession:
    return AdminSession(
        admin=admin_out(admin),
        started_at=record.created_at,
        idle_expires_at=record.last_activity_at + idle_timeout(),
        permissions=permissions_of(admin.role),
    )


# ---------- Lockout ----------


def _countdown(until: datetime, now: datetime) -> str:
    total = max(0, int(-(-(until - now).total_seconds() // 1)))
    return f"{total // 60}:{total % 60:02d}"


def locked_message(until: datetime, now: datetime) -> str:
    return f"Too many attempts. Try again in {_countdown(until, now)}."


def lock_status(db: DB, email: str, now: datetime | None = None) -> LockStatus:
    now = now or now_utc()
    row = db.get(LoginLockout, email.strip().lower())
    if row and row.locked_until and row.locked_until > now:
        return LockStatus(locked=True, until=row.locked_until, attempts_left=0)
    # An expired lock starts a fresh set of attempts.
    failures = 0 if not row or row.locked_until else row.failures
    return LockStatus(locked=False, attempts_left=max(0, MAX_LOGIN_ATTEMPTS - failures))


def _fail_attempt(db: DB, key: str, now: datetime) -> LockStatus:
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


# ---------- Sessions ----------


def sign_in(db: DB, email: str, password: str) -> tuple[str, AdminSession]:
    """A farmer's email gets the same generic error as a wrong password, so the page never
    reveals which accounts exist."""
    now = now_utc()
    key = email.strip().lower()
    lock = lock_status(db, key, now)
    if lock.locked and lock.until:
        raise Locked(locked_message(lock.until, now), code="locked")

    admin = db.scalars(select(Admin).where(Admin.email == key)).first()
    if not verify_password(password, admin.password_hash if admin else None) or not admin:
        after = _fail_attempt(db, key, now)
        db.commit()
        if after.locked and after.until:
            raise Locked(locked_message(after.until, now), code="locked")
        left = after.attempts_left
        raise Unauthorized(
            f"Incorrect email or password. {left} attempt{'' if left == 1 else 's'} left.",
            code="bad_credentials",
        )
    # Only checked after the password matched, so it reveals nothing to a guesser.
    if admin.status != "active":
        raise Forbidden("This admin account is disabled.", code="admin_disabled")

    row = db.get(LoginLockout, key)
    if row:
        db.delete(row)
    admin.last_login_at = now
    token, record = create_session(
        db, "admin", admin_id=admin.id, ttl=timedelta(hours=get_settings().admin_session_hours)
    )
    append_audit(db, admin, action="admin.login", target_type="admin", target_id=admin.id, summary=f"{admin.name} signed in")
    db.commit()
    return token, session_out(admin, record)


def end_admin_session(db: DB, record: AuthSession, admin: Admin | None, reason: str) -> None:
    """Ends the admin session only (logged); the farmer session on the same browser stays."""
    if admin:
        summary = (
            f"{admin.name} was signed out after inactivity" if reason == "timeout" else f"{admin.name} signed out"
        )
        append_audit(db, admin, action="admin.logout", target_type="admin", target_id=admin.id, summary=summary)
    end_session(record)
    db.commit()


def touch(db: DB, record: AuthSession) -> datetime:
    record.last_activity_at = now_utc()
    db.commit()
    return record.last_activity_at


def change_password(db: DB, admin: Admin, current: str, new: str) -> Admin:
    """Also clears a forced change (must_change_password)."""
    if not verify_password(current, admin.password_hash):
        raise AppError("Your current password is incorrect.")
    problem = password_problem(new)
    if problem:
        raise AppError(problem)
    if new == current:
        raise AppError("Choose a password different from your current one.")
    admin.password_hash = hash_password(new)
    admin.must_change_password = False
    append_audit(
        db, admin, action="admin.password_change", target_type="admin", target_id=admin.id,
        summary=f"{admin.name} changed their password",
    )
    db.commit()
    return admin


def rename(db: DB, admin: Admin, name: str) -> Admin:
    """Any role may change its own display name (logged)."""
    clean = " ".join(name.split())
    if len(clean) < 2:
        raise AppError("Enter your full name.")
    if len(clean) > 60:
        raise AppError("Keep your name under 60 characters.")
    if clean == admin.name:
        return admin
    before = admin.name
    admin.name = clean
    append_audit(
        db, admin, action="admin.rename", target_type="admin", target_id=admin.id,
        summary=f"{before} changed their name to {clean}", before={"name": before}, after={"name": clean},
    )
    db.commit()
    return admin


def require_active_session(db: DB, record: AuthSession | None, now: datetime) -> Admin:
    """The admin behind a session, re-read on every request so a role change or a disabled
    account applies at once. Ends (and logs) a session idle past the timeout."""
    if record is None:
        raise Unauthorized(SESSION_ENDED, code="not_signed_in")
    admin = db.get(Admin, record.admin_id) if record.admin_id else None
    if is_idle(record, now):
        end_admin_session(db, record, admin, "timeout")
        raise Unauthorized(SESSION_ENDED, code="session_expired")
    if admin is None or admin.status != "active":
        end_session(record)
        db.commit()
        raise Unauthorized(SESSION_ENDED, code="not_signed_in")
    return admin
