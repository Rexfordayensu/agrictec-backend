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
from app.domain.passwords import farmer_password_problem
from app.integrations import messaging
from app.models import PasswordReset, Profile, User
from app.schemas.farm import Session
from app.services import auth, login_lockout, notifications

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


RESET_UNAVAILABLE = (
    "Password reset isn't available right now: AgriCtec can't send codes yet. "
    "Contact AgriCtec support to get back into your account."
)


def can_send_codes() -> bool:
    """Whether a reset code can reach anyone: email or SMS is set up (while developing, the
    code is written to the API's log instead)."""
    settings = get_settings()
    return settings.email_enabled or settings.sms_enabled or settings.environment != "production"


def request_reset(db: DB, email: str) -> None:
    """Sends a code when the email belongs to an active farmer account; silently does nothing
    otherwise, so the answer can't be used to find out who has an account. When no code can be
    sent at all (no email or SMS set up), it says so to everyone: that reveals nothing about
    accounts, and a farmer isn't left waiting for a code that never comes."""
    if not can_send_codes():
        raise AppError(RESET_UNAVAILABLE, code="reset_unavailable", status_code=503)
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
    if problem := farmer_password_problem(new_password):
        db.rollback()
        raise AppError(problem)
    reset.used_at = now
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    end_sessions_for(db, user_id=user.id)
    # The owner proved who they are: an account-wide sign-in lock (someone guessing) is lifted.
    login_lockout.clear(db, login_lockout.account_key(user.email, "farmer"))
    notice = notifications.create(
        db, user.id, "account", "Your password was changed",
        "You chose a new password with a reset code. If this wasn't you, contact AgriCtec support straight away.",
    )
    token, session = auth._start(db, user)  # Commits (and refuses a suspended account).
    notifications.deliver(notice)
    return token, session


# ---------- A code for changing the password while signed in ----------

EMAIL_UNAVAILABLE = (
    "AgriCtec can't send email codes yet, so your password can't be changed here. "
    "Contact AgriCtec support."
)


def mask_email(email: str) -> str:
    """ama.mensah@gmail.com -> a********@gmail.com: enough to recognise, not to read."""
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'*' * max(3, len(name) - 1)}@{domain}" if domain else email


def send_change_code(db: DB, user: User) -> str:
    """Emails a six-digit code to the account's own email, to change the password while signed
    in: it proves the email is the farmer's. The same codes as a reset (keyed hash, 15 minutes,
    five tries, at most one a minute and five a day). Returns the email, masked."""
    settings = get_settings()
    if not settings.email_enabled and settings.environment == "production":
        raise AppError(EMAIL_UNAVAILABLE, code="email_unavailable", status_code=503)
    now = now_utc()
    latest = _live(db, user.id, lock=True)
    if latest and now - latest.created_at < RESEND_AFTER:
        db.rollback()
        raise AppError("A code was just sent. Wait a minute before asking for another.",
                       code="code_recent", status_code=429)
    today = db.scalar(
        select(func.count())
        .select_from(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.created_at > now - timedelta(days=1))
    )
    if today >= DAILY_CODES:
        db.rollback()
        raise AppError("You have asked for too many codes today. Try again tomorrow.",
                       code="code_limit", status_code=429)
    db.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
        .values(used_at=now)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(PasswordReset(user_id=user.id, code_hash=_digest(user.id, code), created_at=now, expires_at=now + CODE_TTL))
    db.commit()
    if settings.email_enabled:
        threading.Thread(target=_send_change, args=(user.email, code), name="agrictec-change-code", daemon=True).start()
    else:
        # Developing without email: show the code to whoever runs the API locally.
        log.warning("Password change code for %s: %s (email is not configured)", user.email, code)
    return mask_email(user.email)


def _send_change(email: str, code: str) -> None:
    text = (
        f"Your {APP_NAME} code to change your password is {code}. It expires in 15 minutes. "
        "If you did not ask to change your password, someone may know it: reset it and contact AgriCtec support."
    )
    try:
        messaging.send_email(email, f"{APP_NAME} password change code", text)
    except messaging.MessageFailed as error:
        log.warning("Password change code by email failed: %s", error)
    except Exception:  # Never let a gateway take the worker down.
        log.warning("Password change code by email failed", exc_info=True)


def use_code(db: DB, user: User, code: str) -> None:
    """Checks the farmer's latest code and marks it used (not committed). A wrong code counts
    one of its five tries (committed) and raises."""
    reset = _live(db, user.id, lock=True)
    now = now_utc()
    if not reset or reset.expires_at <= now or reset.attempts >= MAX_ATTEMPTS:
        db.rollback()
        raise AppError(BAD_CODE, code="invalid_code")
    if not hmac.compare_digest(reset.code_hash, _digest(user.id, code.strip())):
        reset.attempts += 1
        db.commit()
        raise AppError(BAD_CODE, code="invalid_code")
    reset.used_at = now
