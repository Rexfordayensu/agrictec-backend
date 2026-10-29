"""Verifications (services/admin/verifications.service.ts).

Phone and location checks pass instantly (with an approved record for the admin's history); a
Ghana Card waits in the admin's queue until it is approved. Only a masked card number and a
fingerprint are ever stored."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.errors import AppError, Conflict, NotFound
from app.core.events import bus
from app.domain.clock import now_utc
from app.domain.constants import inside_ghana
from app.domain.verification import (
    AUTOMATIC_REVIEWER,
    GHANA_CARD,
    GHANA_PHONE,
    KIND_LABELS,
    MASKED_CARD,
    REJECT_REASONS,
    hash_ghana_card,
    mask_ghana_card,
    normalize_phone,
    parse_location,
    rejection_message,
)
from app.integrations import identity
from app.models import Admin, Notification, User, VerificationRequest
from app.schemas.farm import VerificationRequest as VerificationOut
from app.services import farm, notifications
from app.services.farmer_data import display_name
from app.services.mappers import verification_out


def _find(db: DB, request_id: str) -> VerificationRequest:
    row = db.get(VerificationRequest, request_id)
    if row is None:
        raise NotFound("Verification request not found.")
    return row


def list_for_farmer(db: DB, user_id: str) -> list[VerificationOut]:
    """The farmer's own requests. The identity check is for reviewers only."""
    rows = db.scalars(
        select(VerificationRequest)
        .where(VerificationRequest.user_id == user_id)
        .order_by(VerificationRequest.submitted_at.desc())
    )
    return [verification_out(r).model_copy(update={"identity_check": None}) for r in rows]


def list_all(db: DB, *, status: str | None = None, kind: str | None = None, user_id: str | None = None) -> list[VerificationOut]:
    query = select(VerificationRequest).order_by(VerificationRequest.submitted_at.desc())
    if status:
        query = query.where(VerificationRequest.status == status)
    if kind:
        query = query.where(VerificationRequest.kind == kind)
    if user_id:
        query = query.where(VerificationRequest.user_id == user_id)
    return [verification_out(r) for r in db.scalars(query)]


def _set_profile_card(db: DB, request: VerificationRequest) -> None:
    """The profile's card follows its latest request: pending, or rejected with the reason."""
    profile = farm.get_profile_row(db, request.user_id)
    rejected = request.status == "rejected"
    profile.ghana_card_status = "rejected" if rejected else "pending"
    profile.ghana_card_masked = request.submitted_value
    profile.ghana_card_hash = request.value_hash
    profile.ghana_card_submitted_at = request.submitted_at
    profile.ghana_card_rejected_reason = rejection_message(request.reason, request.note) if rejected else None


def _apply_approval(db: DB, request: VerificationRequest) -> None:
    """Marks the check as passed on the farmer's profile, and lifts "pending verification"."""
    profile = farm.get_profile_row(db, request.user_id)
    if request.kind == "phone":
        profile.verified_phone = True
        if normalize_phone(profile.phone) != request.submitted_value:
            profile.phone = request.submitted_value
    elif request.kind == "location":
        profile.verified_location = True
        # An approved location also fills in the farm's coordinates if the profile has none.
        point = parse_location(request.submitted_value)
        if point and profile.gps_lat is None and inside_ghana(*point):
            profile.gps_lat, profile.gps_lng = point
    else:
        profile.ghana_card_status = "verified"
        profile.ghana_card_masked = request.submitted_value
        profile.ghana_card_hash = request.value_hash
        profile.ghana_card_submitted_at = request.submitted_at
        profile.ghana_card_rejected_reason = None
        user = db.get(User, request.user_id)
        if user and user.status == "pending_verification":
            user.status = "active"


def queue_card(db: DB, user_id: str, masked: str, value_hash: str | None) -> VerificationRequest:
    """Queues a Ghana Card for review from its mask and fingerprint. Not committed."""
    if not MASKED_CARD.match(masked):
        raise AppError("Enter a valid Ghana Card number.")
    pending = db.scalars(
        select(VerificationRequest.id).where(
            VerificationRequest.user_id == user_id,
            VerificationRequest.kind == "ghanaCard",
            VerificationRequest.status == "pending",
        )
    ).first()
    if pending:
        raise Conflict("Your Ghana Card is already waiting for review.")
    request = VerificationRequest(
        user_id=user_id,
        kind="ghanaCard",
        submitted_value=masked,
        value_hash=value_hash,
        status="pending",
        submitted_at=now_utc(),
    )
    db.add(request)
    _set_profile_card(db, request)
    return request


def card_from_number(card_number: str) -> tuple[str, str | None]:
    """(mask, fingerprint) from a full number, or (mask, None) from the last four digits."""
    value = card_number.strip().upper()
    if GHANA_CARD.match(value):
        return mask_ghana_card("".join(ch for ch in value if ch.isdigit())[-4:]), hash_ghana_card(value)
    if value.isdigit() and len(value) == 4:
        return mask_ghana_card(value), None
    raise AppError("Enter a Ghana Card number such as GHA-123456789-0.")


def submit(db: DB, user_id: str, kind: str, value: str) -> VerificationOut:
    """Called by the farmer app."""
    if kind == "ghanaCard":
        masked, value_hash = card_from_number(value)
        request = queue_card(db, user_id, masked, value_hash)
        # With the full number in hand, ask the NIA register (advice for the reviewer).
        if value_hash and get_settings().identity_checks_enabled:
            name = farm.get_profile_row(db, user_id).name
            request.identity_check = identity.check_ghana_card(value, name, user_id)
    else:
        if kind == "phone":
            if not GHANA_PHONE.match(value.strip()):
                raise AppError("Enter a valid Ghana phone number.")
            submitted = normalize_phone(value)
        else:
            if not value.strip():
                raise AppError("Location is required.")
            submitted = value.strip()
        now = now_utc()
        request = VerificationRequest(
            user_id=user_id,
            kind=kind,
            submitted_value=submitted,
            status="approved",
            submitted_at=now,
            reviewed_by=AUTOMATIC_REVIEWER,
            reviewed_at=now,
        )
        db.add(request)
        _apply_approval(db, request)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("Your Ghana Card is already waiting for review.") from None
    bus.publish("verifications")
    return verification_out(request)


def approve(db: DB, actor: Admin, request_id: str, commit: bool = True) -> VerificationOut:
    request = _find(db, request_id)
    if request.status != "pending":
        raise AppError("This request has already been reviewed.")
    request.status = "approved"
    request.reviewed_by = actor.name
    request.reviewed_at = now_utc()
    _apply_approval(db, request)
    append_audit(
        db,
        actor,
        action="verification.approve",
        target_type="farmer",
        target_id=request.user_id,
        summary=f"Approved the {KIND_LABELS[request.kind]} check for {display_name(db, request.user_id)}",
        before={"status": "pending"},
        after={"status": "approved"},
    )
    notice = notifications.create(
        db, request.user_id, "verification",
        f"Your {KIND_LABELS[request.kind]} is verified",
        "Thank you. This raises your credit score's verification factor."
        + (" You can now apply for loans." if request.kind == "ghanaCard" else ""),
        "/settings/verification",
    )
    if commit:
        db.commit()
        notifications.deliver(notice)
        bus.publish("account", request.user_id)
    return verification_out(request)


def reject(db: DB, actor: Admin, request_id: str, reason: str, note: str = "") -> VerificationOut:
    """The farmer sees "Rejected: <reason>. Please resubmit." and the note, and can submit again."""
    request = _find(db, request_id)
    clean = note.strip()
    if request.status != "pending":
        raise AppError("This request has already been reviewed.")
    if reason not in REJECT_REASONS:
        raise AppError("Choose a reason for the farmer.")
    if reason == "Other" and len(clean) < 3:
        raise AppError("Add a note explaining the reason.")
    if len(clean) > 300:
        raise AppError("Keep the note under 300 characters.")
    request.status = "rejected"
    request.reviewed_by = actor.name
    request.reviewed_at = now_utc()
    request.reason = reason
    request.note = clean or None
    if request.kind == "ghanaCard":
        _set_profile_card(db, request)
    notice = notifications.create(
        db, request.user_id, "verification",
        f"Your {KIND_LABELS[request.kind]} check needs another try",
        rejection_message(reason, request.note),
        "/settings/verification",
    )
    append_audit(
        db,
        actor,
        action="verification.reject",
        target_type="farmer",
        target_id=request.user_id,
        summary=f"Rejected the {KIND_LABELS[request.kind]} check for {display_name(db, request.user_id)}: {reason}",
        before={"status": "pending"},
        after={"status": "rejected", "reason": reason, "note": request.note},
    )
    db.commit()
    notifications.deliver(notice)
    bus.publish("account", request.user_id)
    return verification_out(request)


def approve_many(db: DB, actor: Admin, ids: list[str]) -> list[VerificationOut]:
    """Phone and location checks together (each audited on its own); Ghana Cards one by one."""
    requests = [_find(db, i) for i in ids]
    if any(r.kind == "ghanaCard" for r in requests):
        raise AppError("Ghana Cards must be reviewed one by one.")
    if any(r.status != "pending" for r in requests):
        raise AppError("Some of these requests have already been reviewed.")
    approved = [approve(db, actor, r.id, commit=False) for r in requests]
    notices = [n for n in db.new if isinstance(n, Notification)]
    db.commit()
    notifications.deliver(*notices)
    for user_id in {r.user_id for r in approved}:
        bus.publish("account", user_id)
    return approved
