"""Ghana Card identity verification (app/routers/identity.py).

Two levels, never confused:
- "reviewed": an AgriCtec admin reviewed a typed-in card number (profiles.ghana_card_status =
  "verified", app/services/verifications.py). Not a check of identity against NIA.
- "nia_verified": an identity provider (NIA's approved platform) confirmed the farmer's identity,
  recorded here in identity_verifications. Only a provider's authenticated answer sets it: no
  admin action, frontend value or typed-in number can.

Which level a feature needs is a platform setting (`identity:requirements`), so loans can move to
"nia_verified" once NIA is live without a code change. Simulated (development) results never
count in production.
"""

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.deps import FarmerContext, farmer_context
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.db import get_db
from app.domain.clock import now_utc
from app.integrations.identity_providers import (
    NIA_PENDING_DOCUMENTATION,
    CallbackRejected,
    IdentityProvider,
    ProviderResult,
    ProviderUnavailable,
    TransientProviderError,
)
from app.models import Admin, IdentityVerification, Profile
from app.services import notifications, platform_store

Level = Literal["none", "reviewed", "nia_verified"]
LEVELS: tuple[Level, ...] = ("none", "reviewed", "nia_verified")
OPEN = ("pending", "processing")
FINAL = ("verified", "failed", "expired")

SESSION_MINUTES = 30
MAX_ATTEMPTS_PER_DAY = 3
TRANSIENT_RETRIES = 2  # after the first try, for timeouts only (never a biometric step)

# The only failure codes ever stored or shown: a provider's own reason is mapped onto these.
FAILURE_CODES = {
    "biometric_mismatch": "Your face did not match the Ghana Card record.",
    "not_found": "No matching Ghana Card record was found.",
    "card_invalid": "The Ghana Card could not be used for verification.",
    "session_expired": "The verification session expired before it was completed.",
    "cancelled": "The verification was cancelled.",
    "closed_by_review": "An AgriCtec reviewer closed this check without a verified result.",
    "provider_error": "The verification service could not complete the check.",
}

# Features that may require identity, and what each needs by default.
REQUIREMENTS_KEY = "identity:requirements"
FEATURES = {"loan_application": "Apply for a loan through AgriCtec"}
DEFAULT_REQUIREMENTS: dict[str, Level] = {"loan_application": "reviewed"}

T = TypeVar("T")

# What a farmer is told when verification can't start (configuration details stay private).
UNAVAILABLE_MESSAGES = {
    "not_offered": "Ghana Card identity verification is not available yet.",
    "disabled": NIA_PENDING_DOCUMENTATION,
    "not_configured": NIA_PENDING_DOCUMENTATION,
    "awaiting_documentation": NIA_PENDING_DOCUMENTATION,
    "production": "Ghana Card identity verification is not available yet.",
}


# ---------- Levels and requirements ----------


def _counting(query):
    """Simulated results never count on the live platform."""
    if not get_settings().demo_data_allowed:
        query = query.where(IdentityVerification.provider != "simulated")
    return query


def nia_verified(db: DB, user_id: str) -> IdentityVerification | None:
    return db.scalars(
        _counting(
            select(IdentityVerification).where(
                IdentityVerification.user_id == user_id, IdentityVerification.status == "verified"
            )
        ).order_by(IdentityVerification.verified_at.desc())
    ).first()


def identity_level(db: DB, user_id: str) -> Level:
    if nia_verified(db, user_id):
        return "nia_verified"
    profile = db.get(Profile, user_id)
    return "reviewed" if profile and profile.ghana_card_status == "verified" else "none"


def requirements(db: DB) -> dict[str, Level]:
    saved = platform_store.get_raw(db, REQUIREMENTS_KEY) or {}
    return {
        feature: saved.get(feature) if saved.get(feature) in LEVELS else default
        for feature, default in DEFAULT_REQUIREMENTS.items()
    }


def set_requirements(db: DB, actor: Admin, values: Mapping[str, str]) -> dict[str, Level]:
    unknown = set(values) - set(FEATURES)
    if unknown:
        raise AppError(f"Unknown feature: {', '.join(sorted(unknown))}.")
    if any(v not in LEVELS for v in values.values()):
        raise AppError("Each feature needs one of: none, reviewed, nia_verified.")
    before = requirements(db)
    after = {**before, **values}
    platform_store.put_raw(db, REQUIREMENTS_KEY, after, actor.name)
    append_audit(
        db, actor, action="identity.requirements", target_type="settings", target_id=REQUIREMENTS_KEY,
        summary="Changed which features need Ghana Card identity", before=before, after=after,
    )
    db.commit()
    return after


def meets(level: Level, needed: Level) -> bool:
    return LEVELS.index(level) >= LEVELS.index(needed)


REQUIRED_MESSAGES: dict[Level, str] = {
    "reviewed": "Add your Ghana Card and wait for AgriCtec to review it before you {action}.",
    "nia_verified": "Verify your identity with NIA (Settings → Verification) before you {action}.",
}


def assert_allowed(db: DB, user_id: str, feature: str) -> None:
    """403 "identity_required" when the farmer's level is below what the feature needs."""
    needed = requirements(db).get(feature, "none")
    if needed == "none" or meets(identity_level(db, user_id), needed):
        return
    label = FEATURES.get(feature, "Use this feature")
    action = label[0].lower() + label[1:]
    message = REQUIRED_MESSAGES[needed].format(action=action)
    if needed == "reviewed":
        profile = db.get(Profile, user_id)
        if profile and profile.ghana_card_status == "pending":
            message = f"Your Ghana Card is being reviewed. You can {action} once AgriCtec has reviewed it."
    raise Forbidden(message, code="identity_required", extra={"feature": feature, "required": needed})


def require_identity(feature: str) -> Callable[..., FarmerContext]:
    """A route dependency: the farmer, only when their identity meets the feature's requirement."""

    def dependency(
        ctx: Annotated[FarmerContext, Depends(farmer_context)], db: Annotated[DB, Depends(get_db)]
    ) -> FarmerContext:
        assert_allowed(db, ctx.user.id, feature)
        return ctx

    return dependency


# ---------- The farmer's attempts ----------


def _safe_failure(code: str | None) -> str:
    return code if code in FAILURE_CODES else "provider_error"


def attempt_out(row: IdentityVerification) -> dict[str, Any]:
    """An attempt without anything sensitive (no provider reference, no reviewer note)."""
    return {
        "id": row.id,
        "provider": row.provider,
        "simulated": row.provider == "simulated",
        "status": row.status,
        "method": row.verification_method,
        "initiatedAt": row.initiated_at,
        "verifiedAt": row.verified_at,
        "expiresAt": row.expires_at,
        "failureCode": row.failure_code,
        "failureMessage": FAILURE_CODES.get(row.failure_code or "") if row.failure_code else None,
        "attemptCount": row.attempt_count,
        "consentVersion": row.consent_version,
    }


class GhanaCardVerificationService:
    """Coordinates one farmer's verification with whichever provider is configured."""

    def __init__(self, db: DB, provider: IdentityProvider | None, *, sleep: Callable[[float], None] = time.sleep):
        self.db = db
        self.provider = provider
        self.sleep = sleep

    # -- reading --

    def latest(self, user_id: str) -> IdentityVerification | None:
        return self.db.scalars(
            _counting(select(IdentityVerification).where(IdentityVerification.user_id == user_id))
            .order_by(IdentityVerification.created_at.desc(), IdentityVerification.attempt_count.desc())
        ).first()

    def attempts_today(self, user_id: str, now: datetime) -> int:
        return self.db.scalar(
            _counting(
                select(func.count()).select_from(IdentityVerification).where(
                    IdentityVerification.user_id == user_id,
                    IdentityVerification.created_at >= now - timedelta(days=1),
                )
            )
        ) or 0

    def provider_state(self) -> dict[str, Any]:
        if self.provider is None:
            return {"name": None, "available": False, "reason": "not_offered", "simulated": False}
        reason = self.provider.unavailable_reason()
        return {
            "name": self.provider.name,
            "available": reason is None,
            "reason": reason,
            "simulated": self.provider.simulated,
        }

    def status(self, user_id: str, *, refresh: bool = True) -> dict[str, Any]:
        now = now_utc()
        row = self.latest(user_id)
        if row is not None and refresh:
            self._refresh(row, now)
        level = identity_level(self.db, user_id)
        provider = self.provider_state()
        used = self.attempts_today(user_id, now)
        status = row.status if row else "not_started"
        needed = requirements(self.db)
        return {
            "level": level,
            "status": status,
            "attempt": attempt_out(row) if row else None,
            "provider": provider,
            "canStart": provider["available"] and level != "nia_verified" and row is None,
            "canRetry": provider["available"] and row is not None and row.status in ("failed", "expired")
            and level != "nia_verified" and used < MAX_ATTEMPTS_PER_DAY,
            "attemptsLeftToday": max(0, MAX_ATTEMPTS_PER_DAY - used),
            "consentVersion": get_settings().identity_consent_version,
            "requirements": [
                {"feature": f, "label": FEATURES[f], "required": needed[f], "met": meets(level, needed[f])}
                for f in FEATURES
            ],
        }

    def history(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            _counting(select(IdentityVerification).where(IdentityVerification.user_id == user_id))
            .order_by(IdentityVerification.created_at.desc())
            .limit(50)
        ).all()
        return [attempt_out(r) for r in rows]

    # -- starting --

    def start(
        self, user_id: str, *, consent: bool, consent_version: str, options: Mapping[str, str] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        now = now_utc()
        provider = self._usable_provider()
        settings = get_settings()
        if not consent or consent_version != settings.identity_consent_version:
            raise AppError(
                "Read the privacy notice and confirm your consent before verifying.",
                code="consent_required", extra={"consentVersion": settings.identity_consent_version},
            )
        if identity_level(self.db, user_id) == "nia_verified":
            raise Conflict("Your identity is already verified.", code="already_verified")
        latest = self.latest(user_id)
        if latest is not None:
            self._refresh(latest, now)
            if latest.status in OPEN:
                raise Conflict("A verification is already in progress.", code="in_progress")
            if latest.status == "requires_review":
                raise Conflict("Your last verification is waiting for review.", code="under_review")
            if not retry:
                raise Conflict("Use Try again to start a new verification.", code="retry_required")
        elif retry:
            raise Conflict("There is no verification to try again yet.", code="nothing_to_retry")
        if self.attempts_today(user_id, now) >= MAX_ATTEMPTS_PER_DAY:
            raise AppError(
                "You have reached the number of verification attempts for today. Try again tomorrow.",
                code="max_attempts", status_code=429,
            )

        row = IdentityVerification(
            user_id=user_id, provider=provider.name, status="pending", consent_version=consent_version,
            consent_given_at=now, initiated_at=now, attempt_count=(latest.attempt_count + 1) if latest else 1,
            created_at=now, updated_at=now,
        )
        self.db.add(row)
        try:
            self.db.flush()  # the one-open-session index refuses a concurrent second start
        except IntegrityError:
            self.db.rollback()
            raise Conflict("A verification is already in progress.", code="in_progress") from None
        try:
            session = self._with_retries(
                lambda: provider.start(attempt_id=row.id, user_id=user_id, options=dict(options or {}))
            )
        except ProviderUnavailable as error:
            self.db.rollback()  # nothing is recorded for an attempt that never started
            raise AppError(str(error), code="provider_unavailable", status_code=503,
                           extra={"reason": error.reason}) from None
        except TransientProviderError:
            self.db.rollback()
            raise AppError(
                "The verification service is not responding. Please try again in a few minutes.",
                code="provider_unavailable", status_code=503, extra={"reason": "timeout"},
            ) from None
        row.verification_reference = session.reference
        row.status = "processing"
        row.expires_at = now + timedelta(seconds=session.expires_in_seconds or SESSION_MINUTES * 60)
        row.updated_at = now
        self.db.commit()
        return {
            "attempt": attempt_out(row),
            "instructions": session.instructions,
            "redirectUrl": session.redirect_url,
        }

    def _usable_provider(self) -> IdentityProvider:
        if self.provider is None:
            raise AppError(
                UNAVAILABLE_MESSAGES["not_offered"], code="provider_unavailable",
                status_code=503, extra={"reason": "not_offered"},
            )
        reason = self.provider.unavailable_reason()
        if reason:
            raise AppError(
                UNAVAILABLE_MESSAGES.get(reason, UNAVAILABLE_MESSAGES["not_offered"]),
                code="provider_unavailable", status_code=503, extra={"reason": reason},
            )
        return self.provider

    def _with_retries(self, call: Callable[[], T]) -> T:
        """Bounded retries with exponential backoff, for timeouts only."""
        for attempt in range(TRANSIENT_RETRIES + 1):
            try:
                return call()
            except TransientProviderError:
                if attempt == TRANSIENT_RETRIES:
                    raise
                self.sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable")

    # -- results --

    def _refresh(self, row: IdentityVerification, now: datetime) -> None:
        """Expires a lapsed session, or asks a polling provider for its answer."""
        if row.status not in OPEN:
            return
        if row.expires_at and row.expires_at <= now:
            row.status, row.failure_code, row.updated_at = "expired", "session_expired", now
            self.db.commit()
            return
        provider = self.provider
        if (
            provider is None or provider.supports_callback or provider.name != row.provider
            or not row.verification_reference or provider.unavailable_reason()
        ):
            return
        try:
            result = self._with_retries(lambda: provider.result(row.verification_reference or ""))
        except (ProviderUnavailable, TransientProviderError):
            return  # left as it is; the farmer sees it still in progress
        self.apply(row, result, now)

    def apply(self, row: IdentityVerification, result: ProviderResult, now: datetime | None = None) -> bool:
        """Records a provider's answer once (idempotent). True when it changed the attempt."""
        now = now or now_utc()
        if result.event_id and result.event_id == row.last_event_id:
            return False  # the same event again
        if row.status in FINAL:
            return False  # a settled attempt never changes
        if result.status == row.status:
            return False
        if result.status == "processing":
            return False
        row.status = result.status
        row.verification_method = (result.method or row.verification_method or "")[:60] or None
        row.last_event_id = result.event_id or row.last_event_id
        row.updated_at = now
        if result.status == "verified":
            row.verified_at, row.failure_code = now, None
            title, body = "Identity verified", "Your Ghana Card identity check is complete."
        elif result.status == "requires_review":
            title, body = "Identity check under review", "Your Ghana Card check needs a closer look. We will let you know."
        else:
            row.failure_code = _safe_failure(result.failure_code if result.status == "failed" else "session_expired")
            title, body = "Identity check not completed", "Your Ghana Card check did not complete. You can try again."
        notifications.create(self.db, row.user_id, "verification", title, body, href="/settings/verification")
        self.db.commit()
        return True

    def handle_callback(self, headers: Mapping[str, str], body: bytes) -> str:
        """A provider callback: authenticated by the provider adapter, applied once."""
        provider = self.provider
        if provider is None or not provider.supports_callback or provider.unavailable_reason():
            raise NotFound("Callbacks are not enabled.", code="callback_not_supported")
        try:
            result = provider.parse_callback(headers, body)
        except CallbackRejected:
            raise AppError("Callback rejected.", code="callback_rejected", status_code=401) from None
        row = self.db.scalars(
            select(IdentityVerification).where(
                IdentityVerification.provider == provider.name,
                IdentityVerification.verification_reference == result.reference,
            )
        ).first()
        if row is None:
            raise NotFound("Unknown verification.", code="unknown_reference")
        return "applied" if self.apply(row, result) else "ignored"


# ---------- Admin review ----------


def review_queue(db: DB) -> list[dict[str, Any]]:
    from app.services.farmer_data import display_name

    rows = db.scalars(
        select(IdentityVerification)
        .where(IdentityVerification.status == "requires_review")
        .order_by(IdentityVerification.updated_at)
    ).all()
    return [{**attempt_out(r), "userId": r.user_id, "farmer": display_name(db, r.user_id)} for r in rows]


def close_review(db: DB, actor: Admin, attempt_id: str, note: str) -> dict[str, Any]:
    """An admin closes a check the provider left for review. It can only end as not verified:
    only the provider can verify an identity."""
    row = db.get(IdentityVerification, attempt_id)
    if row is None:
        raise NotFound("Verification not found.")
    if row.status != "requires_review":
        raise Conflict("Only a verification waiting for review can be closed.", code="not_under_review")
    note = note.strip()
    if len(note) < 3:
        raise AppError("Say why the check is being closed.")
    before = {"status": row.status}
    now = now_utc()
    row.status, row.failure_code, row.reviewed_by, row.review_note, row.updated_at = (
        "failed", "closed_by_review", actor.name, note[:300], now,
    )
    append_audit(
        db, actor, action="identity.review_closed", target_type="farmer", target_id=row.user_id,
        summary="Closed a Ghana Card identity check as not verified", before=before,
        after={"status": "failed", "failureCode": "closed_by_review"},
    )
    notifications.create(
        db, row.user_id, "verification", "Identity check not completed",
        "Your Ghana Card check could not be completed. You can try again.", href="/settings/verification",
    )
    db.commit()
    return attempt_out(row)
