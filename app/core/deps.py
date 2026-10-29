"""Who is calling: the request dependencies every protected endpoint uses."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DB

from app.core.errors import (
    READ_ONLY_MESSAGE,
    SESSION_ENDED,
    SUSPENDED_MESSAGE,
    AppError,
    Forbidden,
    Unauthorized,
)
from app.core.sessions import (
    ADMIN_COOKIE,
    FARMER_COOKIE,
    IMPERSONATION_HEADER,
    bearer_token,
    end_session,
    find_session,
)
from app.db import get_db
from app.domain.clock import now_utc
from app.domain.permissions import can, permission_message
from app.models import Admin, AuthSession, User
from app.services import admin_auth, platform_store

ACTIVITY_INTERVAL = timedelta(minutes=1)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class FarmerContext:
    user: User
    session: AuthSession
    # True while an admin views the app as this farmer: every write is refused.
    read_only: bool = False
    admin: Admin | None = None


@dataclass
class AdminContext:
    admin: Admin
    session: AuthSession


def _impersonation(request: Request, db: DB, now) -> FarmerContext | None:
    """The read-only view, valid only while the admin who opened it is still signed in on this
    browser, active and allowed to view as a farmer."""
    record = find_session(db, request.headers.get(IMPERSONATION_HEADER), ("impersonation",), now)
    if record is None:
        return None
    admin_session = find_session(db, request.cookies.get(ADMIN_COOKIE), ("admin",), now)
    admin = db.get(Admin, record.admin_id) if record.admin_id else None
    user = db.get(User, record.user_id) if record.user_id else None
    if (
        admin_session is None
        or admin_session.admin_id != record.admin_id
        or admin_auth.is_idle(admin_session, now)
        or admin is None
        or admin.status != "active"
        or not can(admin.role, "farmers.impersonate")
        or user is None
    ):
        end_session(record, now)
        db.commit()
        return None
    return FarmerContext(user=user, session=record, read_only=True, admin=admin)


def farmer_context(request: Request, db: DB = Depends(get_db)) -> FarmerContext:
    now = now_utc()
    viewing = _impersonation(request, db, now)
    if viewing:
        return viewing

    token = request.cookies.get(FARMER_COOKIE) or bearer_token(request)
    record = find_session(db, token, ("farmer",), now)
    if record is None:
        raise Unauthorized(SESSION_ENDED, code="not_signed_in")
    user = db.get(User, record.user_id) if record.user_id else None
    if user is None:
        # An admin deleted the account: its open session ends too.
        end_session(record, now)
        db.commit()
        raise Unauthorized(SESSION_ENDED, code="account_deleted")
    if user.status == "suspended":
        end_session(record, now)
        db.commit()
        raise Forbidden(SUSPENDED_MESSAGE, code="account_suspended")
    flags = platform_store.flags(db)
    if flags.maintenance_mode:
        raise AppError(flags.maintenance_message, code="maintenance", status_code=503)
    # "Last active" for the admin, written at most once a minute.
    if not user.last_active_at or now - user.last_active_at >= ACTIVITY_INTERVAL:
        user.last_active_at = now
        record.last_activity_at = now
        db.commit()
    return FarmerContext(user=user, session=record)


def writable_farmer(ctx: FarmerContext = Depends(farmer_context)) -> FarmerContext:
    if ctx.read_only:
        raise Forbidden(READ_ONLY_MESSAGE, code="read_only")
    return ctx


def admin_context(request: Request, db: DB = Depends(get_db)) -> AdminContext:
    now = now_utc()
    token = request.cookies.get(ADMIN_COOKIE) or bearer_token(request)
    record = find_session(db, token, ("admin",), now)
    admin = admin_auth.require_active_session(db, record, now)
    assert record is not None
    # Changes count as activity (committed with them); reads such as polling never keep an idle
    # console signed in. The console reports keyboard and mouse activity with POST /touch.
    if request.method not in SAFE_METHODS:
        record.last_activity_at = now
    return AdminContext(admin=admin, session=record)


def require(permission: str) -> Callable[..., AdminContext]:
    """A dependency that lets only admins with `permission` through. An admin still on a
    temporary password can do nothing until they change it."""

    def dependency(ctx: AdminContext = Depends(admin_context)) -> AdminContext:
        if not can(ctx.admin.role, permission):
            raise Forbidden(permission_message(permission), code="forbidden")
        if ctx.admin.must_change_password:
            raise Forbidden("Change your temporary password before continuing.", code="password_change_required")
        return ctx

    return dependency
