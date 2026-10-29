"""Farmer sign-up, sign-in and guest access (services/auth.service.ts)."""

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.errors import SUSPENDED_MESSAGE, AppError, Conflict, Forbidden, Unauthorized
from app.core.security import hash_password, sign, unsign, verify_password
from app.core.sessions import create_session, end_session
from app.domain.clock import now_utc
from app.domain.passwords import FARMER_PASSWORD_MIN_LENGTH
from app.models import AuthSession, User
from app.schemas.farm import Session
from app.services import platform_store

GUEST_EMAIL = "guest@agrictec.demo"
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
BAD_CREDENTIALS = "Incorrect email or password"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def session_ttl() -> timedelta:
    return timedelta(days=get_settings().farmer_session_days)


def session_out(user: User, record: AuthSession, read_only: bool = False) -> Session:
    return Session(
        user_id=user.id,
        email=user.email,
        is_guest=user.is_guest,
        started_at=record.created_at,
        read_only=read_only,
    )


def _start(db: DB, user: User) -> tuple[str, Session]:
    if user.status == "suspended":
        raise Forbidden(SUSPENDED_MESSAGE, code="account_suspended")
    user.last_active_at = now_utc()
    token, record = create_session(db, "farmer", user_id=user.id, ttl=session_ttl())
    db.commit()
    return token, session_out(user, record)


def _find(db: DB, email: str) -> User | None:
    return db.scalars(select(User).where(User.email == normalize_email(email), User.is_guest.is_(False))).first()


def sign_up(db: DB, email: str, password: str) -> tuple[str, Session]:
    if not platform_store.flags(db).signup_open:
        raise Forbidden("New sign-ups are closed right now.", code="signup_closed")
    normalized = normalize_email(email)
    if not EMAIL.match(normalized):
        raise AppError("Enter a valid email address.")
    if len(password) < FARMER_PASSWORD_MIN_LENGTH:
        raise AppError(f"Password must be at least {FARMER_PASSWORD_MIN_LENGTH} characters")
    if _find(db, normalized):
        raise Conflict("An account with this email already exists")
    user = User(email=normalized, password_hash=hash_password(password), is_guest=False, created_at=now_utc())
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise Conflict("An account with this email already exists") from None
    return _start(db, user)


def sign_in(db: DB, email: str, password: str) -> tuple[str, Session]:
    user = _find(db, email)
    if not verify_password(password, user.password_hash if user else None) or not user:
        raise Unauthorized(BAD_CREDENTIALS, code="bad_credentials")
    if user.status != "suspended" and user.must_change_password:
        # An admin reset the password: the farmer chooses a new one before any session starts.
        raise Conflict("Choose a new password to finish signing in.", code="password_change_required")
    return _start(db, user)


def replace_temporary_password(db: DB, email: str, temporary: str, new_password: str) -> tuple[str, Session]:
    """After an admin reset: checks the temporary password again, saves the farmer's own new
    password (the admin never learns it) and signs them in."""
    user = _find(db, email)
    if not user or not verify_password(temporary, user.password_hash):
        raise Unauthorized(BAD_CREDENTIALS, code="bad_credentials")
    if len(new_password) < FARMER_PASSWORD_MIN_LENGTH:
        raise AppError(f"Password must be at least {FARMER_PASSWORD_MIN_LENGTH} characters")
    if new_password == temporary:
        raise AppError("Choose a password different from the temporary one.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    return _start(db, user)


def continue_as_guest(db: DB, device_cookie: str | None) -> tuple[str, Session, str]:
    """One guest per device: the guest's ID lives in a signed, long-lived cookie, so its demo data
    never vanishes between visits. Returns the session token, the session and the device cookie."""
    if not platform_store.flags(db).guest_mode:
        raise Forbidden("Guest mode is turned off right now.", code="guest_mode_off")
    guest_id = unsign(device_cookie)
    guest = db.get(User, guest_id) if guest_id else None
    if guest is None or not guest.is_guest:
        guest = User(email=GUEST_EMAIL, password_hash=None, is_guest=True, created_at=now_utc())
        db.add(guest)
        db.flush()
    token, session = _start(db, guest)
    return token, session, sign(guest.id)


def sign_out(db: DB, record: AuthSession) -> None:
    end_session(record)
    db.commit()
