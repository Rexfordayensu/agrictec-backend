"""Announcements (services/admin/announcements.service.ts)."""

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError, NotFound
from app.core.events import bus
from app.domain.announcements import can_delete, can_edit, can_end, needs_critical_confirm
from app.domain.clock import now_utc
from app.models import Admin, Announcement
from app.schemas.platform import Announcement as AnnouncementOut
from app.schemas.platform import AnnouncementCreate, AnnouncementInput, AnnouncementPatch
from app.services.mappers import announcement_out

CRITICAL_CONFIRM = "Confirm sending a critical announcement to every farmer."


def _find(db: DB, announcement_id: str) -> Announcement:
    row = db.get(Announcement, announcement_id)
    if row is None:
        raise NotFound("Announcement not found.")
    return row


def _input_of(a: AnnouncementOut) -> dict:
    return a.model_dump(mode="json", by_alias=True, include=set(AnnouncementInput.model_fields))


def list_all(db: DB) -> list[AnnouncementOut]:
    """Newest first."""
    return [announcement_out(r) for r in db.scalars(select(Announcement).order_by(Announcement.created_at.desc()))]


def create(db: DB, actor: Admin, data: AnnouncementCreate) -> AnnouncementOut:
    """A critical announcement to everyone needs confirmedCritical (the form asks first)."""
    if needs_critical_confirm(data) and not data.confirmed_critical:
        raise AppError(CRITICAL_CONFIRM, code="confirm_critical")
    row = Announcement(
        title=data.title,
        message=data.message,
        type=data.type,
        audience=data.audience.model_dump(mode="json"),
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        active=data.active,
        dismissible=data.dismissible,
        created_by=actor.name,
        created_at=now_utc(),
    )
    db.add(row)
    db.flush()
    out = announcement_out(row)
    append_audit(db, actor, action="announcement.create", target_type="announcement", target_id=row.id,
                 summary=f"Created announcement “{row.title}”", after=out)
    db.commit()
    bus.publish("platform")
    return out


def update(db: DB, actor: Admin, announcement_id: str, patch: AnnouncementPatch) -> AnnouncementOut:
    """Only before it starts: afterwards, end it and duplicate it."""
    row = _find(db, announcement_id)
    current = announcement_out(row)
    if not can_edit(current, now_utc()):
        raise AppError("Only scheduled announcements can be edited. End it and duplicate it instead.")
    changes = patch.model_dump(mode="json", by_alias=True, exclude_unset=True, exclude={"confirmed_critical"})
    data = AnnouncementInput.model_validate({**_input_of(current), **changes})
    if needs_critical_confirm(data) and not needs_critical_confirm(current) and not patch.confirmed_critical:
        raise AppError(CRITICAL_CONFIRM, code="confirm_critical")
    row.title, row.message, row.type = data.title, data.message, data.type
    row.audience = data.audience.model_dump(mode="json")
    row.starts_at, row.ends_at = data.starts_at, data.ends_at
    row.active, row.dismissible = data.active, data.dismissible
    after = announcement_out(row)
    append_audit(db, actor, action="announcement.update", target_type="announcement", target_id=announcement_id,
                 summary=f"Edited announcement “{current.title}”", before=_input_of(current), after=_input_of(after))
    db.commit()
    bus.publish("platform")
    return after


def end(db: DB, actor: Admin, announcement_id: str) -> AnnouncementOut:
    """Takes it down now, keeping it in the list."""
    row = _find(db, announcement_id)
    now = now_utc()
    if not can_end(announcement_out(row), now):
        raise AppError("This announcement has already ended.")
    before = {"active": row.active, "endsAt": row.ends_at}
    row.active = False
    row.ends_at = row.ends_at if row.ends_at and row.ends_at < now else now
    append_audit(db, actor, action="announcement.end", target_type="announcement", target_id=announcement_id,
                 summary=f"Ended announcement “{row.title}”", before=before,
                 after={"active": False, "endsAt": row.ends_at})
    db.commit()
    bus.publish("platform")
    return announcement_out(row)


def remove(db: DB, actor: Admin, announcement_id: str) -> None:
    row = _find(db, announcement_id)
    current = announcement_out(row)
    if not can_delete(current, now_utc()):
        raise AppError("Farmers are seeing this announcement now. End it before deleting.")
    db.delete(row)
    append_audit(db, actor, action="announcement.delete", target_type="announcement", target_id=announcement_id,
                 summary=f"Deleted announcement “{current.title}”", before=current, after={"deleted": True})
    db.commit()
    bus.publish("platform")
