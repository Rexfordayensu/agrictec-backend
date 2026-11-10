"""The farmer's account as a whole: credit summary, onboarding, announcements and the farmer's
own data export, import and deletion (services/onboarding.service.ts, lib/data/backup.ts)."""

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DB

from app.core.errors import AppError
from app.domain.announcements import by_severity, in_audience, is_live
from app.domain.clock import now_utc, parse_iso
from app.domain.credit import calculate_credit_score, score_label
from app.domain.farming import build_readiness
from app.domain.finance import annualized_net_profit
from app.domain.loans import borrowing_limit
from app.models import (
    Announcement,
    ChatMessage,
    Diagnosis,
    FarmRecord,
    MomoTransaction,
    OnboardingDraft,
    Profile,
    RiskFlag,
    User,
    UserSettings,
    VerificationRequest,
)
from app.schemas import farm as f
from app.schemas.platform import Announcement as AnnouncementOut
from app.services import farm, lending, platform_store, verifications
from app.services.farmer_data import load_farmer
from app.services.mappers import announcement_out


def under_review(db: DB, user_id: str) -> bool:
    """Whether an admin confirmed a risk flag on this account (no details leave the console)."""
    return bool(
        db.scalar(select(func.count()).where(RiskFlag.user_id == user_id, RiskFlag.status == "confirmed"))
    )


def credit_summary(db: DB, user_id: str) -> f.CreditSummary:
    data = load_farmer(db, user_id)
    config = platform_store.credit_config(db)
    credit = calculate_credit_score(data.records, data.momo, data.profile, config)
    card_pending = bool(
        db.scalar(
            select(func.count()).where(
                VerificationRequest.user_id == user_id,
                VerificationRequest.kind == "ghanaCard",
                VerificationRequest.status == "pending",
            )
        )
    )
    annualized = annualized_net_profit(data.records)
    return f.CreditSummary(
        credit=credit,
        band=score_label(credit.score, config.bands),
        readiness=build_readiness(
            records=data.records, momo=data.momo, profile=data.profile,
            min_records=config.eligibility.min_records, card_under_review=card_pending,
        ),
        annualized_profit=annualized,
        max_affordable=borrowing_limit(annualized, config.eligibility),
        under_review=under_review(db, user_id),
    )


# ---------- Announcements ----------


def live_announcements(db: DB) -> list[AnnouncementOut]:
    """Every live announcement, most severe first, for any audience."""
    now = now_utc()
    items = [announcement_out(r) for r in db.scalars(select(Announcement))]
    return by_severity([a for a in items if is_live(a, now)])


def my_announcements(db: DB, user_id: str) -> list[AnnouncementOut]:
    """Live, for this farmer's region and crops, and not dismissed."""
    profile = farm.get_profile(db, user_id)
    gone = set(farm.dismissed(db, user_id))
    return [
        a
        for a in live_announcements(db)
        if a.id not in gone and in_audience(a.audience, profile.region, profile.primary_crops)
    ]


# ---------- Onboarding ----------


def complete_onboarding(db: DB, user: User, data: f.OnboardingComplete) -> f.Profile:
    """Saves the whole profile and opens the app. The farm's pin and a Ghana Card go to the
    verifications queue (pending until an admin approves them)."""
    row = farm.get_profile_row(db, user.id)
    name, email = row.name, row.email
    farm.apply_profile_fields(row, data.profile)
    row.name = name or data.profile.name.strip()
    row.email = email or data.profile.email.strip() or ("" if user.is_guest else user.email)
    db.flush()
    if row.gps_lat is not None and not row.verified_location:
        verifications.queue_location(db, user.id, f"{row.gps_lat:.4f}, {row.gps_lng:.4f}")

    card = data.ghana_card
    if card and card.card_number:
        if row.ghana_card_status not in ("pending", "verified"):
            verifications.queue_full_card(db, user.id, card.card_number)
    elif row.ghana_card_status in ("not_provided", "rejected"):
        row.ghana_card_status = "not_provided"
        row.ghana_card_masked = row.ghana_card_hash = row.ghana_card_rejected_reason = None
        row.ghana_card_submitted_at = None

    row.onboarding_completed_at = now_utc()
    farm.clear_draft(db, user.id, commit=False)
    db.commit()
    return farm.get_profile(db, user.id)


# ---------- The farmer's own data ----------


def export_data(db: DB, user_id: str) -> f.DataExport:
    diagnoses = [d.model_copy(update={"image_thumb": None}) for d in farm.list_diagnoses(db, user_id)]
    return f.DataExport(
        exported_at=now_utc(),
        profile=farm.get_profile(db, user_id),
        records=farm.list_records(db, user_id),
        momo=farm.list_momo(db, user_id),
        diagnoses=diagnoses,
        chat=farm.list_chat(db, user_id),
        settings=farm.get_settings_for(db, user_id),
        loan_applications=lending.list_for_farmer(db, user_id),
    )


def _clear_own_data(db: DB, user_id: str, *, keep_ai_history: bool = False) -> None:
    models = (FarmRecord, MomoTransaction, UserSettings, OnboardingDraft)
    if not keep_ai_history:
        models += (Diagnosis, ChatMessage)
    for model in models:
        db.execute(delete(model).where(model.user_id == user_id))


def import_data(db: DB, user_id: str, data: f.DataImport) -> None:
    """Replaces the farmer's records, MoMo, settings and editable profile, all or nothing. The
    Ghana Card, verifications, admin notes, loan applications, diagnoses and the assistant
    conversation are kept: they are written by the platform, never taken from a file."""
    try:
        fields = f.ProfileFields.model_validate(data.profile)
    except ValueError as error:
        raise AppError("The profile in this file is invalid.") from error
    _clear_own_data(db, user_id, keep_ai_history=True)
    now = now_utc()
    row = farm.get_profile_row(db, user_id)
    farm.apply_profile_fields(row, fields)
    completed = data.profile.get("onboardingCompletedAt")
    if completed and not row.onboarding_completed_at:
        try:
            f.OnboardingProfile.model_validate(data.profile)
            row.onboarding_completed_at = parse_iso(completed)
        except ValueError:
            pass  # An incomplete profile goes through onboarding again.
    db.add_all(FarmRecord(user_id=user_id, created_at=now, **r.model_dump()) for r in data.records)
    for t in data.momo:
        values = t.model_dump()
        values["reference"] = values["reference"].strip() or "MOMO000000"
        db.add(MomoTransaction(user_id=user_id, created_at=now, **values))
    db.flush()
    farm.save_settings(db, user_id, data.settings, commit=False)
    db.commit()


def delete_data(db: DB, user_id: str, confirmation: str) -> None:
    """Deletes everything the farmer entered; they set up their farm again afterwards. Their
    sign-in, Ghana Card status, verification history and loan applications stay (the phone and
    location checks went with the phone and pin)."""
    if confirmation != "DELETE":
        raise AppError("Type DELETE to confirm.")
    _clear_own_data(db, user_id)
    row = db.get(Profile, user_id)
    if row:
        kept = {
            k: getattr(row, k)
            for k in (
                "ghana_card_status", "ghana_card_masked", "ghana_card_hash", "ghana_card_submitted_at",
                "ghana_card_rejected_reason",
            )
        }
        db.delete(row)
        db.flush()
        db.add(Profile(user_id=user_id, **kept))
    db.commit()
