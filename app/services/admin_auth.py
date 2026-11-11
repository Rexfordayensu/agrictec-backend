"""Admin sign-in, sessions and the lockout (services/admin/admin-auth.service.ts).

The lockout (app/services/login_lockout.py) counts failures per email AND visitor address: five
in a row lock that pair for five minutes. Someone else guessing at an admin's email only locks
themselves out, never the admin (the per-address rate limit on /admin/auth/login caps guessing
from any one address too).

Farmer and admin sessions are separate: neither one ever signs the other out."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.errors import SESSION_ENDED, AppError, Forbidden, Unauthorized
from app.core.security import hash_password, verify_password
from app.core.sessions import create_session, end_session
from app.domain.clock import now_utc
from app.domain.passwords import password_problem
from app.domain.permissions import permissions_of
from app.models import Admin, AuthSession
from app.schemas.admin import AdminSession
from app.services import login_lockout
from app.services.mappers import admin_out


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


# ---------- Sessions ----------


def sign_in(db: DB, email: str, password: str, client: str = "unknown") -> tuple[str, AdminSession]:
    """A farmer's email gets the same generic error as a wrong password, so the page never
    reveals which accounts exist. `client` is the visitor's address (the lockout's other half)."""
    now = now_utc()
    address = email.strip().lower()
    key = login_lockout.lockout_key(address, client)
    lock = login_lockout.lock_status(db, key, now)
    if lock.locked and lock.until:
        raise login_lockout.locked_error(lock.until, now)

    admin = db.scalars(select(Admin).where(Admin.email == address)).first()
    if not verify_password(password, admin.password_hash if admin else None) or not admin:
        after = login_lockout.fail_attempt(db, key, now)
        db.commit()
        if after.locked and after.until:
            raise login_lockout.locked_error(after.until, now)
        raise Unauthorized("Incorrect email or password.", code="bad_credentials")
    # Only checked after the password matched, so it reveals nothing to a guesser.
    if admin.status != "active":
        raise Forbidden("This admin account is disabled.", code="admin_disabled")

    login_lockout.clear(db, key)
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
