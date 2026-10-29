"""Notices to farmers about what happened to their account: a Ghana Card or location decision,
a loan's next step, a suspension, a password reset. Always in the app's inbox (the bell); also by
SMS and email when those are configured.

create() adds the notice to the caller's transaction; deliver() then sends it outside the app in
the background, after the commit, and records how that went."""

import logging
import threading

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.db import SessionLocal
from app.domain.clock import now_utc
from app.integrations import messaging
from app.models import Notification, Profile, User

log = logging.getLogger("agrictec.notifications")
APP_NAME = "AgriCtec"


def create(db: DB, user_id: str, kind: str, title: str, body: str, href: str | None = None) -> Notification:
    row = Notification(user_id=user_id, kind=kind, title=title, body=body, href=href, created_at=now_utc())
    db.add(row)
    return row


def deliver(*rows: Notification) -> None:
    """Sends the notices by SMS and email, in the background (call after the commit)."""
    settings = get_settings()
    ids = [r.id for r in rows if r is not None]
    if ids and (settings.sms_enabled or settings.email_enabled):
        threading.Thread(target=_send, args=(ids,), name="agrictec-notify", daemon=True).start()


def _send(ids: list[str]) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        for row in db.scalars(select(Notification).where(Notification.id.in_(ids))):
            user = db.get(User, row.user_id)
            profile = db.get(Profile, row.user_id)
            text = f"{APP_NAME}: {row.title}. {row.body}"
            if settings.sms_enabled:
                row.sms_status = _attempt(lambda: messaging.send_sms(profile.phone if profile else "", text))
            if settings.email_enabled and user:
                address = (profile.email if profile and profile.email else "") or user.email
                link = f"{settings.public_app_url.rstrip('/')}{row.href or '/dashboard'}"
                row.email_status = _attempt(
                    lambda: messaging.send_email(address, f"{APP_NAME}: {row.title}", f"{row.body}\n\n{link}\n")
                )
        db.commit()


def _attempt(send) -> str:
    try:
        send()
        return "sent"
    except messaging.MessageFailed as error:
        return f"failed: {error}"[:200]
    except Exception as error:  # Never let a gateway take the worker down.
        log.warning("Notification delivery failed", exc_info=True)
        return f"failed: {type(error).__name__}"[:200]


# ---------- The inbox ----------


def inbox(db: DB, user_id: str, limit: int = 50) -> dict:
    rows = db.scalars(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    ).all()
    unread = db.scalar(
        select(func.count()).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return {
        "unread": unread or 0,
        "items": [
            {
                "id": r.id,
                "kind": r.kind,
                "title": r.title,
                "body": r.body,
                "href": r.href,
                "createdAt": r.created_at,
                "read": r.read_at is not None,
            }
            for r in rows
        ],
    }


def mark_read(db: DB, user_id: str, ids: list[str] | None) -> None:
    query = update(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
    if ids:
        query = query.where(Notification.id.in_(ids))
    db.execute(query.values(read_at=now_utc()))
    db.commit()
