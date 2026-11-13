"""A farmer's own data (records, MoMo, diagnoses, chat, settings, profile and onboarding).
Every query is scoped to the signed-in farmer: nobody reads or changes another farmer's data."""

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DB

from app.core.errors import AppError, NotFound
from app.core.events import bus
from app.domain.clock import now_utc, today
from app.domain.verification import normalize_phone
from app.domain.transactions import transition
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
from app.services import mappers, transactions

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
    moved = data.gps is not None and row.gps_lat is not None and (
        abs(data.gps.lat - row.gps_lat) >= 0.00006 or abs(data.gps.lng - (row.gps_lng or 0)) >= 0.00006
    )
    if (data.gps is None and row.gps_lat is not None) or moved:
        # Removing or moving the map pin un-verifies the farm location (an admin reviews it again).
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
    return transactions.entry(db, "farm", record_id, user_id, lock=True)


def list_records(db: DB, user_id: str) -> list[f.FarmRecord]:
    """Newest first, by date and then by creation time."""
    rows = list(db.scalars(
        select(FarmRecord)
        .where(FarmRecord.user_id == user_id)
        .order_by(FarmRecord.date.desc(), FarmRecord.created_at.desc())
    ))
    metadata = mappers.transaction_metadata_many(db, rows, "farm")
    return [mappers.record_out(r, metadata=metadata[r.id]) for r in rows]


def _check_date(value) -> None:
    """An entry describes something that happened: at most a day ahead (time zones). A date in
    the future would sit in the score's history window before it ever happened."""
    if value is not None and value > today() + timedelta(days=1):
        raise AppError("The date can't be in the future.")


def create_record(db: DB, user_id: str, data: f.RecordInput) -> f.FarmRecord:
    _check_date(data.date)
    values = _record_fields(data.model_dump())
    transactions.prepare_write(db, user_id, "farm", values, confirm_duplicate=data.confirm_duplicate)
    row = FarmRecord(user_id=user_id, created_at=now_utc(), **values)
    db.add(row)
    db.flush()
    transactions.change_status(db, row, "farm", transition(None, "create"), initial=True)
    db.commit()
    bus.publish("account", user_id)
    return mappers.record_out(row)


def _record_fields(values: dict) -> dict:
    # Activities carry no amount.
    if values.get("type") == "activity":
        values["amount"] = 0
        values["category"] = None
        values["direction"] = None
    return values


def update_record(db: DB, user_id: str, record_id: str, patch: f.RecordPatch) -> f.FarmRecord:
    row = _record(db, user_id, record_id)
    changes = patch.model_dump(exclude_unset=True)
    _check_date(changes.get("date"))
    changes = _record_fields(changes) if changes.get("type", row.type) == "activity" else changes
    transactions.prepare_write(db, user_id, "farm", changes, row=row, confirm_duplicate=patch.confirm_duplicate)
    for key, value in changes.items():
        if value is None and key in {"amount", "type", "date", "crop", "description", "source_type"}:
            raise AppError(f"{key} cannot be empty.")
        setattr(row, key, value)
    row.updated_at = now_utc()
    db.commit()
    bus.publish("account", user_id)
    return mappers.record_out(row)


def delete_record(db: DB, user_id: str, record_id: str) -> None:
    transactions.delete_entry(db, "farm", _record(db, user_id, record_id))
    db.commit()
    bus.publish("account", user_id)


def replace_records(db: DB, user_id: str, items: list[f.RecordInput]) -> list[f.FarmRecord]:
    """Replaces every record (sample data and "clear all")."""
    for existing in db.scalars(select(FarmRecord).where(FarmRecord.user_id == user_id).with_for_update()).all():
        transactions.delete_entry(db, "farm", existing)
    now = now_utc()
    db.add_all(FarmRecord(user_id=user_id, created_at=now, **_record_fields(i.model_dump())) for i in items)
    db.commit()
    bus.publish("account", user_id)
    return list_records(db, user_id)


# ---------- MoMo ----------


def _momo(db: DB, user_id: str, momo_id: str) -> MomoTransaction:
    row = db.get(MomoTransaction, momo_id)
    if row is None or row.user_id != user_id:
        raise NotFound("Transaction not found")
    return transactions.entry(db, "momo", momo_id, user_id, lock=True)


def momo_summary(db: DB, user_id: str) -> dict:
    """The MoMo page's totals (app/domain/momo_summary.py), from the farmer's saved entries."""
    from app.domain import momo_summary as summary
    from app.domain.clock import today

    rows = list(db.scalars(select(MomoTransaction).where(MomoTransaction.user_id == user_id)))
    return summary.build(rows, today())


def list_momo(db: DB, user_id: str) -> list[f.MomoTransaction]:
    rows = list(db.scalars(
        select(MomoTransaction)
        .where(MomoTransaction.user_id == user_id)
        .order_by(MomoTransaction.date.desc(), MomoTransaction.created_at.desc())
    ))
    metadata = mappers.transaction_metadata_many(db, rows, "momo")
    return [mappers.momo_out(r, metadata=metadata[r.id]) for r in rows]


def create_momo(db: DB, user_id: str, data: f.MomoInput) -> f.MomoTransaction:
    _check_date(data.date)
    values = data.model_dump()
    values["reference"] = (values["reference"] or "").strip() or None
    transactions.prepare_write(db, user_id, "momo", values, confirm_duplicate=data.confirm_duplicate)
    row = MomoTransaction(user_id=user_id, created_at=now_utc(), **values)
    db.add(row)
    db.flush()
    transactions.change_status(db, row, "momo", transition(None, "create"), initial=True)
    db.commit()
    bus.publish("account", user_id)
    return mappers.momo_out(row)


def update_momo(db: DB, user_id: str, momo_id: str, patch: f.MomoPatch) -> f.MomoTransaction:
    row = _momo(db, user_id, momo_id)
    _check_date(patch.date)
    changes = patch.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key in {"amount", "direction", "date", "counterparty", "source_type"}:
            raise AppError(f"{key} cannot be empty.")
    transactions.prepare_write(db, user_id, "momo", changes, row=row, confirm_duplicate=patch.confirm_duplicate)
    if "reference" in changes and changes["reference"] != row.reference:
        row.reference_duplicate = False
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_at = now_utc()
    db.commit()
    bus.publish("account", user_id)
    return mappers.momo_out(row)


def delete_momo(db: DB, user_id: str, momo_id: str) -> None:
    transactions.delete_entry(db, "momo", _momo(db, user_id, momo_id))
    db.commit()
    bus.publish("account", user_id)


def replace_momo(db: DB, user_id: str, items: list[f.MomoInput]) -> list[f.MomoTransaction]:
    for existing in db.scalars(select(MomoTransaction).where(MomoTransaction.user_id == user_id).with_for_update()).all():
        transactions.delete_entry(db, "momo", existing)
    now = now_utc()
    for item in items:
        values = item.model_dump()
        values["reference"] = (values["reference"] or "").strip() or None
        db.add(MomoTransaction(user_id=user_id, created_at=now, **values))
    db.commit()
    bus.publish("account", user_id)
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


def append_chat(db: DB, user_id: str, messages: list[f.ChatMessage]) -> None:
    """Adds messages the API wrote (the assistant's question and answer) to the end of the
    conversation, keeping the most recent MAX_CHAT_MESSAGES. Not committed: the caller commits
    with the credit it spent. The browser never writes the conversation."""
    last = db.scalar(select(func.max(ChatMessage.position)).where(ChatMessage.user_id == user_id))
    position = -1 if last is None else last
    for m in messages:
        position += 1
        content = m.content if isinstance(m.content, str) else m.content.model_dump(by_alias=True, exclude_none=True)
        db.add(
            ChatMessage(
                id=m.id, user_id=user_id, position=position, role=m.role, content=content, created_at=m.created_at
            )
        )
    db.flush()
    cutoff = position - MAX_CHAT_MESSAGES
    if cutoff >= 0:
        db.execute(delete(ChatMessage).where(ChatMessage.user_id == user_id, ChatMessage.position <= cutoff))


def clear_chat(db: DB, user_id: str) -> None:
    db.execute(delete(ChatMessage).where(ChatMessage.user_id == user_id))
    db.commit()


def _sms_available() -> bool:
    """Whether an SMS gateway is configured, so farmers can choose SMS alerts at all."""
    from app.config import get_settings

    return get_settings().sms_enabled


def get_settings_for(db: DB, user_id: str) -> f.AppSettings:
    sms = _sms_available()
    row = db.get(UserSettings, user_id)
    if row is None:
        return f.AppSettings(sms_available=sms)
    return f.AppSettings(
        language=row.language,
        notifications=f.NotificationSettings(**(row.notifications or {})),
        sms_available=sms,
    )


def save_settings(db: DB, user_id: str, data: f.AppSettings, commit: bool = True) -> f.AppSettings:
    row = db.get(UserSettings, user_id) or UserSettings(user_id=user_id)
    row.language = data.language
    row.notifications = data.notifications.model_dump()
    db.merge(row)
    if commit:
        db.commit()
    return data.model_copy(update={"sms_available": _sms_available()})


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
