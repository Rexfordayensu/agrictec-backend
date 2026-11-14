"""Farmer sign-up and sign-in (services/auth.service.ts). Guests have no account: the demo
runs in the web app only and nothing of it is stored here."""

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.errors import SUSPENDED_MESSAGE, AppError, Conflict, Forbidden, Unauthorized
from app.core.security import hash_password, verify_password
from app.core.sessions import create_session, end_session
from app.domain.clock import now_utc
from app.domain.passwords import farmer_password_problem
from app.models import AuthSession, User
from app.schemas.farm import Session
from app.services import login_lockout, platform_store

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
    if problem := farmer_password_problem(password):
        raise AppError(problem)
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


def _check_password(db: DB, email: str, password: str, client: str) -> User | None:
    """The user when the password is right. Five wrong ones in a row from one address lock that
    email there for five minutes (423 with when it ends); a wrong one says only that it's wrong."""
    now = now_utc()
    key = login_lockout.lockout_key(email, client, scope="farmer")
    lock = login_lockout.lock_status(db, key, now)
    if lock.locked and lock.until:
        raise login_lockout.locked_error(lock.until, now)
    user = _find(db, email)
    if not verify_password(password, user.password_hash if user else None) or not user:
        after = login_lockout.fail_attempt(db, key, now)
        db.commit()
        if after.locked and after.until:
            raise login_lockout.locked_error(after.until, now)
        raise Unauthorized(BAD_CREDENTIALS, code="bad_credentials")
    login_lockout.clear(db, key)
    return user


def sign_in(db: DB, email: str, password: str, client: str = "unknown") -> tuple[str, Session]:
    user = _check_password(db, email, password, client)
    if user.status != "suspended" and user.must_change_password:
        # An admin reset the password: the farmer chooses a new one before any session starts.
        raise Conflict("Choose a new password to finish signing in.", code="password_change_required")
    return _start(db, user)


def replace_temporary_password(
    db: DB, email: str, temporary: str, new_password: str, client: str = "unknown"
) -> tuple[str, Session]:
    """After an admin reset: checks the temporary password again, saves the farmer's own new
    password (the admin never learns it) and signs them in."""
    user = _check_password(db, email, temporary, client)
    if problem := farmer_password_problem(new_password):
        raise AppError(problem)
    if new_password == temporary:
        raise AppError("Choose a password different from the temporary one.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    return _start(db, user)


def sign_out(db: DB, record: AuthSession) -> None:
    end_session(record)
    db.commit()
