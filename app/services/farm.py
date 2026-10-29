"""A farmer's own data (records, MoMo, diagnoses, chat, settings, profile and onboarding).
Every query is scoped to the signed-in farmer: nobody reads or changes another farmer's data."""

import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DB

from app.core.errors import AppError, NotFound
from app.domain.clock import now_utc
from app.domain.verification import normalize_phone
from app.models import (
    Announcement,
    ChatMessage,
    Diagnosis,
    DismissedAnnouncement,
    FarmRecord,
    MomoTransaction,
    OnboardingDraft,
    Profile,
    UserSettings,
)
from app.schemas import farm as f
from app.services import mappers

MAX_DIAGNOSES = 10
MAX_CHAT_MESSAGES = 200

# ---------- Profile ----------


def get_profile_row(db: DB, user_id: str) -> Profile:
    row = db.get(Profile, user_id)
    if row is None:
        row = Profile(user_id=user_id)
        db.add(row)
        db.flush()
    return row


def get_profile(db: DB, user_id: str) -> f.Profile:
    return mappers.profile_out(db.get(Profile, user_id), user_id)


def _text(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def apply_profile_fields(row: Profile, data: f.ProfileFields) -> None:
    """The farmer's editable fields, cleaned as the frontend's safeProfile() cleans them."""
    phone = data.phone.strip()
    if normalize_phone(phone) != normalize_phone(row.phone):
        # A verified phone stays verified only for the number that was checked.
        row.verified_phone = False
    row.name = data.name.strip()
    row.photo_data_url = data.photo_data_url
    row.email = data.email.strip()
    row.phone = phone
    row.farm_name = data.farm_name.strip()
    row.use_name_as_farm_name = data.use_name_as_farm_name
    row.region = data.region
    row.constituency = data.constituency
    row.community = _text(data.community)
    if data.gps is None and row.gps_lat is not None:
        # Removing the map pin un-verifies the farm location.
        row.verified_location = False
    row.gps_lat = data.gps.lat if data.gps else None
    row.gps_lng = data.gps.lng if data.gps else None
    row.acres = data.acres
    row.size_unit = data.size_unit
    row.primary_crops = list(data.primary_crops)
    row.other_crops = _text(data.other_crops) if "Other" in data.primary_crops else None
    row.farm_type = data.farm_type
    row.cooperative_name = _text(data.cooperative_name) if data.farm_type == "Cooperative" else None
    row.outgrower_company = _text(data.outgrower_company) if data.farm_type == "Out-grower" else None


def save_profile(db: DB, user_id: str, data: f.ProfileFields) -> f.Profile:
    row = get_profile_row(db, user_id)
    apply_profile_fields(row, data)
    db.commit()
    return mappers.profile_out(row, user_id)


# ---------- Records ----------


def _record(db: DB, user_id: str, record_id: str) -> FarmRecord:
    row = db.get(FarmRecord, record_id)
    if row is None or row.user_id != user_id:
        raise NotFound("Record not found")
    return row


def list_records(db: DB, user_id: str) -> list[f.FarmRecord]:
    """Newest first, by date and then by creation time."""
    rows = db.scalars(
        select(FarmRecord)
        .where(FarmRecord.user_id == user_id)
        .order_by(FarmRecord.date.desc(), FarmRecord.created_at.desc())
    )
    return [mappers.record_out(r) for r in rows]


def create_record(db: DB, user_id: str, data: f.RecordInput) -> f.FarmRecord:
    row = FarmRecord(user_id=user_id, created_at=now_utc(), **_record_fields(data.model_dump()))
    db.add(row)
    db.commit()
    return mappers.record_out(row)


def _record_fields(values: dict) -> dict:
    # Activities carry no amount.
    if values.get("type") == "activity":
        values["amount"] = 0
    return values


def update_record(db: DB, user_id: str, record_id: str, patch: f.RecordPatch) -> f.FarmRecord:
    row = _record(db, user_id, record_id)
    changes = patch.model_dump(exclude_unset=True)
    for key, value in _record_fields({"type": changes.get("type", row.type), **changes}).items():
        setattr(row, key, value)
    db.commit()
    return mappers.record_out(row)


def delete_record(db: DB, user_id: str, record_id: str) -> None:
    db.delete(_record(db, user_id, record_id))
    db.commit()


def replace_records(db: DB, user_id: str, items: list[f.RecordInput]) -> list[f.FarmRecord]:
    """Replaces every record (sample data and "clear all")."""
    db.execute(delete(FarmRecord).where(FarmRecord.user_id == user_id))
    now = now_utc()
    db.add_all(FarmRecord(user_id=user_id, created_at=now, **_record_fields(i.model_dump())) for i in items)
    db.commit()
    return list_records(db, user_id)


# ---------- MoMo ----------


def _momo(db: DB, user_id: str, momo_id: str) -> MomoTransaction:
    row = db.get(MomoTransaction, momo_id)
    if row is None or row.user_id != user_id:
        raise NotFound("Transaction not found")
    return row


def _reference(value: str) -> str:
    """A blank reference becomes "MOMO" and six digits."""
    return value.strip() or f"MOMO{secrets.randbelow(900_000) + 100_000}"


def list_momo(db: DB, user_id: str) -> list[f.MomoTransaction]:
    rows = db.scalars(
        select(MomoTransaction)
        .where(MomoTransaction.user_id == user_id)
        .order_by(MomoTransaction.date.desc(), MomoTransaction.created_at.desc())
    )
    return [mappers.momo_out(r) for r in rows]


def create_momo(db: DB, user_id: str, data: f.MomoInput) -> f.MomoTransaction:
    values = data.model_dump()
    values["reference"] = _reference(values["reference"])
    row = MomoTransaction(user_id=user_id, created_at=now_utc(), **values)
    db.add(row)
    db.commit()
    return mappers.momo_out(row)


def update_momo(db: DB, user_id: str, momo_id: str, patch: f.MomoPatch) -> f.MomoTransaction:
    row = _momo(db, user_id, momo_id)
    for key, value in patch.model_dump(exclude_unset=True).items():
        setattr(row, key, value.strip() if key == "reference" else value)
    db.commit()
    return mappers.momo_out(row)


def delete_momo(db: DB, user_id: str, momo_id: str) -> None:
    db.delete(_momo(db, user_id, momo_id))
    db.commit()


def replace_momo(db: DB, user_id: str, items: list[f.MomoInput]) -> list[f.MomoTransaction]:
    db.execute(delete(MomoTransaction).where(MomoTransaction.user_id == user_id))
    now = now_utc()
    for item in items:
        values = item.model_dump()
        values["reference"] = _reference(values["reference"])
        db.add(MomoTransaction(user_id=user_id, created_at=now, **values))
    db.commit()
    return list_momo(db, user_id)


# ---------- Diagnoses ----------


def list_diagnoses(db: DB, user_id: str) -> list[f.Diagnosis]:
    rows = db.scalars(
        select(Diagnosis)
        .where(Diagnosis.user_id == user_id)
        .order_by(Diagnosis.date.desc(), Diagnosis.created_at.desc())
    )
    return [mappers.diagnosis_out(r) for r in rows]


def create_diagnosis(db: DB, user_id: str, data: f.DiagnosisInput) -> f.Diagnosis:
    """Puts the new diagnosis first and keeps only the 10 most recent."""
    row = Diagnosis(user_id=user_id, created_at=now_utc(), **data.model_dump())
    db.add(row)
    db.flush()
    keep = db.scalars(
        select(Diagnosis.id)
        .where(Diagnosis.user_id == user_id)
        .order_by(Diagnosis.date.desc(), Diagnosis.created_at.desc())
        .limit(MAX_DIAGNOSES)
    ).all()
    db.execute(delete(Diagnosis).where(Diagnosis.user_id == user_id, Diagnosis.id.not_in(keep)))
    db.commit()
    return mappers.diagnosis_out(row)


def delete_diagnosis(db: DB, user_id: str, diagnosis_id: str) -> None:
    db.execute(delete(Diagnosis).where(Diagnosis.user_id == user_id, Diagnosis.id == diagnosis_id))
    db.commit()


def clear_diagnoses(db: DB, user_id: str) -> None:
    db.execute(delete(Diagnosis).where(Diagnosis.user_id == user_id))
    db.commit()


# ---------- Chat and settings ----------


def list_chat(db: DB, user_id: str) -> list[f.ChatMessage]:
    rows = db.scalars(select(ChatMessage).where(ChatMessage.user_id == user_id).order_by(ChatMessage.position))
    return [f.ChatMessage(id=r.id, role=r.role, content=r.content, created_at=r.created_at) for r in rows]


def save_chat(db: DB, user_id: str, messages: list[f.ChatMessage], commit: bool = True) -> None:
    """Replaces the conversation (the most recent messages are kept)."""
    db.execute(delete(ChatMessage).where(ChatMessage.user_id == user_id))
    db.flush()
    kept = messages[-MAX_CHAT_MESSAGES:]
    seen: set[str] = set()
    for position, m in enumerate(kept):
        if m.id in seen:
            raise AppError("Each chat message needs its own ID.")
        seen.add(m.id)
        content = m.content if isinstance(m.content, str) else m.content.model_dump(by_alias=True, exclude_none=True)
        db.add(
            ChatMessage(
                id=m.id,
                user_id=user_id,
                position=position,
                role=m.role,
                content=content,
                created_at=m.created_at,
            )
        )
    if commit:
        db.commit()


def clear_chat(db: DB, user_id: str) -> None:
    db.execute(delete(ChatMessage).where(ChatMessage.user_id == user_id))
    db.commit()


def get_settings_for(db: DB, user_id: str) -> f.AppSettings:
    row = db.get(UserSettings, user_id)
    if row is None:
        return f.AppSettings()
    return f.AppSettings(language=row.language, notifications=f.NotificationSettings(**(row.notifications or {})))


def save_settings(db: DB, user_id: str, data: f.AppSettings, commit: bool = True) -> f.AppSettings:
    row = db.get(UserSettings, user_id) or UserSettings(user_id=user_id)
    row.language = data.language
    row.notifications = data.notifications.model_dump()
    db.merge(row)
    if commit:
        db.commit()
    return data


# ---------- Onboarding drafts ----------


def _clean_draft(data: dict) -> dict:
    """Never a raw Ghana Card number in a draft: only its status, mask and fingerprint."""
    card = data.get("ghanaCard")
    clean = {k: v for k, v in data.items() if k not in ("ghanaCard", "cardNumber", "ghanaCardNumber")}
    if isinstance(card, dict):
        clean["ghanaCard"] = {k: card[k] for k in ("status", "masked", "hash") if card.get(k)}
    return clean


def get_draft(db: DB, user_id: str) -> f.OnboardingDraft | None:
    row = db.get(OnboardingDraft, user_id)
    if row is None:
        return None
    return f.OnboardingDraft(user_id=user_id, current_step=row.current_step, data=row.data, updated_at=row.updated_at)


def save_draft(db: DB, user_id: str, data: f.OnboardingDraftInput) -> f.OnboardingDraft:
    if len(str(data.data)) > 300_000:
        raise AppError("This draft is too large to save.")
    row = db.get(OnboardingDraft, user_id) or OnboardingDraft(user_id=user_id)
    row.current_step = data.current_step
    row.data = _clean_draft(data.data)
    row.updated_at = now_utc()
    db.merge(row)
    db.commit()
    return f.OnboardingDraft(user_id=user_id, current_step=row.current_step, data=row.data, updated_at=row.updated_at)


def clear_draft(db: DB, user_id: str, commit: bool = True) -> None:
    db.execute(delete(OnboardingDraft).where(OnboardingDraft.user_id == user_id))
    if commit:
        db.commit()


# ---------- Announcement dismissals ----------


def dismissed(db: DB, user_id: str) -> list[str]:
    return list(
        db.scalars(select(DismissedAnnouncement.announcement_id).where(DismissedAnnouncement.user_id == user_id))
    )


def dismiss(db: DB, user_id: str, announcement_id: str) -> list[str]:
    if db.get(Announcement, announcement_id) is None:
        raise NotFound("Announcement not found.")
    if db.get(DismissedAnnouncement, (user_id, announcement_id)) is None:
        db.add(DismissedAnnouncement(user_id=user_id, announcement_id=announcement_id, at=now_utc()))
        db.commit()
    return dismissed(db, user_id)
