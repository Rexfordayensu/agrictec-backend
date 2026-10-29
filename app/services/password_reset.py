"""A farmer's own password reset: a six-digit code by email (and SMS to a verified phone), then a
new password.

The answer to a request never says whether the email has an account. Codes are kept only as a
keyed hash, expire after 15 minutes, allow five attempts, and a newer request replaces them (five
codes per account a day at most).
Using one ends every open session and leaves a notice in the farmer's inbox."""

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.core.sessions import end_sessions_for
from app.domain.clock import now_utc
from app.domain.passwords import FARMER_PASSWORD_MIN_LENGTH
from app.integrations import messaging
from app.models import PasswordReset, Profile, User
from app.schemas.farm import Session
from app.services import auth, notifications

log = logging.getLogger("agrictec.password_reset")

CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5
# A second request within this time sends nothing (the first code still works).
RESEND_AFTER = timedelta(seconds=60)
# Codes per account per day, so guessing can't go on by asking for code after code.
DAILY_CODES = 5
BAD_CODE = "That code is wrong or has expired. Request a new one."
APP_NAME = "AgriCtec"


def _digest(user_id: str, code: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _live(db: DB, user_id: str, *, lock: bool = False) -> PasswordReset | None:
    query = (
        select(PasswordReset)
        .where(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
        .order_by(PasswordReset.created_at.desc())
        .limit(1)
    )
    return db.scalars(query.with_for_update() if lock else query).first()


def channels(db: DB, user: User) -> list[str]:
    """Where a code can go for this account: "email" and/or "sms"."""
    settings = get_settings()
    profile = db.get(Profile, user.id)
    out = []
    if settings.email_enabled:
        out.append("email")
    if settings.sms_enabled and profile and profile.phone and profile.verified_phone:
        out.append("sms")
    return out


def request_reset(db: DB, email: str) -> None:
    """Sends a code when the email belongs to an active farmer account; silently does nothing
    otherwise, so the answer can't be used to find out who has an account."""
    user = auth._find(db, email)
    if not user or user.status == "suspended":
        return
    now = now_utc()
    latest = _live(db, user.id, lock=True)
    if latest and now - latest.created_at < RESEND_AFTER:
        return
    today = db.scalar(
        select(func.count())
        .select_from(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.created_at > now - timedelta(days=1))
    )
    if today >= DAILY_CODES:
        db.rollback()
        return
    db.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
        .values(used_at=now)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(PasswordReset(user_id=user.id, code_hash=_digest(user.id, code), created_at=now, expires_at=now + CODE_TTL))
    db.commit()
    routes = channels(db, user)
    if routes:
        profile = db.get(Profile, user.id)
        threading.Thread(
            target=_send,
            args=(routes, user.email, profile.phone if profile else "", code),
            name="agrictec-reset",
            daemon=True,
        ).start()
    elif get_settings().environment == "development":
        # No email or SMS set up: show the code to whoever runs the API locally.
        log.warning("Password reset code for %s: %s (email and SMS are not configured)", user.email, code)


def _send(routes: list[str], email: str, phone: str, code: str) -> None:
    text = f"Your {APP_NAME} password reset code is {code}. It expires in 15 minutes. If you didn't ask for it, ignore this message."
    for route in routes:
        try:
            if route == "email":
                messaging.send_email(email, f"{APP_NAME} password reset code", text)
            else:
                messaging.send_sms(phone, text)
        except messaging.MessageFailed as error:
            log.warning("Reset code by %s failed: %s", route, error)
        except Exception:  # Never let a gateway take the worker down.
            log.warning("Reset code by %s failed", route, exc_info=True)


def confirm_reset(db: DB, email: str, code: str, new_password: str) -> tuple[str, Session]:
    """Checks the code, saves the new password, ends the old sessions and signs the farmer in."""
    user = auth._find(db, email)
    if not user:
        raise AppError(BAD_CODE, code="invalid_code")
    reset = _live(db, user.id, lock=True)
    now = now_utc()
    if not reset or reset.expires_at <= now or reset.attempts >= MAX_ATTEMPTS:
        db.rollback()
        raise AppError(BAD_CODE, code="invalid_code")
    if not hmac.compare_digest(reset.code_hash, _digest(user.id, code.strip())):
        reset.attempts += 1
        db.commit()
        raise AppError(BAD_CODE, code="invalid_code")
    if len(new_password) < FARMER_PASSWORD_MIN_LENGTH:
        db.rollback()
        raise AppError(f"Password must be at least {FARMER_PASSWORD_MIN_LENGTH} characters")
    reset.used_at = now
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    end_sessions_for(db, user_id=user.id)
    notice = notifications.create(
        db, user.id, "account", "Your password was changed",
        "You chose a new password with a reset code. If this wasn't you, contact AgriCtec support straight away.",
    )
    token, session = auth._start(db, user)  # Commits (and refuses a suspended account).
    notifications.deliver(notice)
    return token, session
