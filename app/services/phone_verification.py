"""Proving a farmer owns their phone number: a six-digit code by SMS, then the code back.

Codes are kept only as a keyed hash bound to the number they were sent to, expire after 10
minutes, allow five attempts, and a newer request replaces them (five codes per account a day at
most). Without an SMS gateway (ARKESEL_API_KEY) phones can't be verified in production; while
developing, the code is written to the API's log instead."""

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.errors import AppError
from app.domain.clock import now_utc
from app.domain.verification import GHANA_PHONE, normalize_phone
from app.integrations import messaging
from app.models import PhoneCode
from app.services import farm

log = logging.getLogger("agrictec.phone_verification")

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
DAILY_CODES = 5
BAD_CODE = "That code is wrong or has expired. Request a new one."
APP_NAME = "AgriCtec"


def _digest(user_id: str, phone: str, code: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"phone:{user_id}:{phone}:{code}".encode(), hashlib.sha256).hexdigest()


def _live(db: DB, user_id: str, *, lock: bool = False) -> PhoneCode | None:
    query = (
        select(PhoneCode)
        .where(PhoneCode.user_id == user_id, PhoneCode.used_at.is_(None))
        .order_by(PhoneCode.created_at.desc())
        .limit(1)
    )
    return db.scalars(query.with_for_update() if lock else query).first()


def _masked(phone: str) -> str:
    return f"{phone[:-7]}****{phone[-3:]}" if len(phone) > 7 else phone


def send_code(db: DB, user_id: str) -> dict:
    """Texts a code to the phone number saved on the farmer's profile."""
    settings = get_settings()
    profile = farm.get_profile_row(db, user_id)
    raw = (profile.phone or "").strip()
    if not GHANA_PHONE.match(raw):
        raise AppError("Save a valid Ghana phone number in Personal information first.")
    phone = normalize_phone(raw)
    if not settings.sms_enabled and settings.environment == "production":
        raise AppError(
            "Phone verification by SMS isn't available yet. Please try again later.",
            code="sms_unavailable",
            status_code=503,
        )
    now = now_utc()
    latest = _live(db, user_id, lock=True)
    if latest and latest.phone == phone and now - latest.created_at < RESEND_AFTER:
        db.rollback()
        raise AppError("A code was just sent. Wait a minute before asking for another.", code="code_recently_sent", status_code=429)
    today = db.scalar(
        select(func.count())
        .select_from(PhoneCode)
        .where(PhoneCode.user_id == user_id, PhoneCode.created_at > now - timedelta(days=1))
    )
    if today >= DAILY_CODES:
        db.rollback()
        raise AppError("Too many codes today. Try again tomorrow.", code="too_many_codes", status_code=429)
    db.execute(
        update(PhoneCode).where(PhoneCode.user_id == user_id, PhoneCode.used_at.is_(None)).values(used_at=now)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        PhoneCode(
            user_id=user_id, phone=phone, code_hash=_digest(user_id, phone, code), created_at=now, expires_at=now + CODE_TTL
        )
    )
    db.commit()
    text = f"Your {APP_NAME} verification code is {code}. It expires in 10 minutes. Never share it with anyone."
    if settings.sms_enabled:
        try:
            messaging.send_sms(phone, text)
        except messaging.MessageFailed as error:
            log.warning("Phone code by SMS failed: %s", error)
            raise AppError(
                "We couldn't send the code by SMS just now. Please try again.", code="sms_failed", status_code=502
            ) from error
    elif settings.environment == "development":
        # No SMS gateway while developing: show the code to whoever runs the API locally.
        log.warning("Phone verification code for %s: %s (SMS is not configured)", phone, code)
    return {"sentTo": _masked(phone), "expiresInMinutes": int(CODE_TTL.total_seconds() // 60)}


def confirm_code(db: DB, user_id: str, phone: str, code: str) -> None:
    """Checks the code for this number and uses it up. Not committed on success."""
    number = normalize_phone(phone.strip())
    record = _live(db, user_id, lock=True)
    now = now_utc()
    if not record or record.phone != number or record.expires_at <= now or record.attempts >= MAX_ATTEMPTS:
        db.rollback()
        raise AppError(BAD_CODE, code="invalid_code")
    if not hmac.compare_digest(record.code_hash, _digest(user_id, number, code.strip())):
        record.attempts += 1
        db.commit()
        raise AppError(BAD_CODE, code="invalid_code")
    record.used_at = now
