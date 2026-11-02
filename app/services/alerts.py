"""Error alerts on admins' phones (web push, app/integrations/push.py).

Admins who can manage settings turn alerts on per device (System settings > Error alerts). An
unhandled error in a request or in the background jobs sends one alert per kind of error at most
every 15 minutes; repeats in between are counted into the next one. Alerts name the route and the
error's type, never request data, so no farmer's details leave the server.

Sending runs on a background thread, so a failing request isn't slowed down, and the devices are
remembered in memory, so an alert still goes out while the database is down."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.errors import AppError
from app.db import SessionLocal
from app.domain.clock import now_utc
from app.domain.permissions import can
from app.integrations import push
from app.models import Admin, PushSubscription

log = logging.getLogger(__name__)

REPEAT_EVERY = timedelta(minutes=15)
# Browsers' own push services. Devices are only ever contacted there.
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")
GONE = (404, 410)


@dataclass(frozen=True)
class Device:
    id: str
    endpoint: str
    p256dh: str
    auth: str


_lock = threading.Lock()
# Kind of error -> (when it was last sent, how many repeats were held back since).
_sent: dict[str, tuple[datetime, int]] = {}
_known: list[Device] = []
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agrictec-alerts")


def _devices(db: DB, admin_id: str | None = None) -> list[Device]:
    query = select(PushSubscription, Admin.role).join(Admin).where(Admin.status == "active")
    if admin_id:
        query = query.where(PushSubscription.admin_id == admin_id)
    return [
        Device(row.id, row.endpoint, row.p256dh, row.auth)
        for row, role in db.execute(query)
        if can(role, "settings.manage")
    ]


def refresh_devices() -> None:
    """Remembers who gets alerts, for when the database can't be read."""
    global _known
    try:
        with SessionLocal() as db:
            _known = _devices(db)
    except Exception:
        log.warning("Could not read the devices for error alerts", exc_info=True)


def _deliver(devices: list[Device], message: dict) -> tuple[int, int]:
    """(sent, failed). Devices whose subscription has ended are forgotten."""
    sent, gone = 0, []
    for device in devices:
        status = push.send(device.endpoint, device.p256dh, device.auth, message)
        if 200 <= status < 300:
            sent += 1
        elif status in GONE:
            gone.append(device.id)
        else:
            log.warning("An error alert was not delivered (push service answered %s)", status)
    if gone:
        try:
            with SessionLocal() as db:
                db.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
                db.commit()
            refresh_devices()
        except Exception:
            log.warning("Could not forget ended alert subscriptions", exc_info=True)
    return sent, len(devices) - sent


def _send_to_all(message: dict) -> None:
    refresh_devices()
    _deliver(list(_known), message)


def report(kind: str) -> None:
    """Alerts every admin who turned alerts on; `kind` names the error and groups its repeats
    ("POST /api/v1/me/records failed: KeyError"). Never raises."""
    now = now_utc()
    with _lock:
        last, held = _sent.get(kind, (None, 0))
        if last and now - last < REPEAT_EVERY:
            _sent[kind] = (last, held + 1)
            return
        _sent[kind] = (now, 0)
    more = f" It happened {held} more {'time' if held == 1 else 'times'} since the last alert." if held else ""
    message = {
        "title": "AgriCtec: something went wrong",
        "body": f"{kind}.{more} The details are in the server logs.",
        "tag": kind[:100],
        "url": "/admin",
    }
    try:
        if get_settings().environment == "test":
            _send_to_all(message)
        else:
            _executor.submit(_send_to_all, message)
    except Exception:  # An alert must never make things worse.
        log.warning("Could not send an error alert", exc_info=True)


# ---------- An admin's own devices ----------


def status(db: DB, admin: Admin) -> dict:
    return {"publicKey": push.public_key(), "devices": len(_devices(db, admin.id))}


def _valid(endpoint: str, p256dh: str, auth: str) -> None:
    url = urlsplit(endpoint)
    host = url.hostname or ""
    if url.scheme != "https" or not any(host == h or host.endswith(f".{h}") for h in PUSH_HOSTS):
        raise AppError("This browser's push service isn't supported.")
    try:
        ok = len(push.b64url_decode(p256dh)) == 65 and len(push.b64url_decode(auth)) == 16
    except ValueError:
        ok = False
    if not ok:
        raise AppError("This browser sent invalid push keys.")


def subscribe(db: DB, admin: Admin, endpoint: str, p256dh: str, auth: str) -> dict:
    _valid(endpoint, p256dh, auth)
    row = db.scalars(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
    if row is None:
        row = PushSubscription(admin_id=admin.id, endpoint=endpoint, p256dh=p256dh, auth=auth, created_at=now_utc())
        db.add(row)
    else:  # The same device again (or now signed in as someone else).
        row.admin_id, row.p256dh, row.auth = admin.id, p256dh, auth
    append_audit(
        db, admin, action="alerts.subscribe", target_type="system", target_id="error-alerts",
        summary="Turned on error alerts on a device",
    )
    db.commit()
    refresh_devices()
    return status(db, admin)


def unsubscribe(db: DB, admin: Admin, endpoint: str) -> dict:
    removed = db.execute(
        delete(PushSubscription).where(PushSubscription.endpoint == endpoint, PushSubscription.admin_id == admin.id)
    ).rowcount
    if removed:
        append_audit(
            db, admin, action="alerts.unsubscribe", target_type="system", target_id="error-alerts",
            summary="Turned off error alerts on a device",
        )
    db.commit()
    refresh_devices()
    return status(db, admin)


def send_test(db: DB, admin: Admin) -> dict:
    devices = _devices(db, admin.id)
    if not devices:
        raise AppError("Turn on alerts on this device first.")
    sent, failed = _deliver(
        devices,
        {
            "title": "AgriCtec: test alert",
            "body": "Error alerts work on this device. You'll get one like this if something breaks.",
            "tag": "test",
            "url": "/admin/settings/alerts",
        },
    )
    return {"sent": sent, "failed": failed}
