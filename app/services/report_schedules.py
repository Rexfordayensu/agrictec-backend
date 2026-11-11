"""Reports emailed on a schedule: a console report's CSV every Monday or every 1st of the month,
at 07:00 Ghana time (UTC, no daylight saving).

A background thread in each API worker looks for due schedules every minute. Rows are claimed
with FOR UPDATE SKIP LOCKED, so with several workers each report still goes out once."""

import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.errors import AppError, NotFound
from app.core.ratelimit import purge_stale
from app.db import SessionLocal
from app.domain.clock import iso, now_utc
from app.domain.reports import REPORT_CATALOG, report_file_name, report_to_csv
from app.integrations import messaging
from app.models import Admin, ReportSchedule
from app.services import alerts, farm_alerts, insights

log = logging.getLogger("agrictec.report_schedules")

SEND_HOUR = 7
MAX_RECIPIENTS = 10
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CHECK_EVERY_SECONDS = 60
TITLES = {r["id"]: r["title"] for r in REPORT_CATALOG}


def next_run(frequency: str, after: datetime) -> datetime:
    """The next send time strictly after `after`: Monday 07:00 or the 1st at 07:00 (UTC)."""
    after = after.astimezone(timezone.utc)
    if frequency == "weekly":
        day = after.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
        day += timedelta(days=(7 - day.weekday()) % 7)
        return day if day > after else day + timedelta(days=7)
    first = after.replace(day=1, hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if first > after:
        return first
    return first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)


def out(row: ReportSchedule) -> dict:
    return {
        "id": row.id,
        "reportId": row.report_id,
        "reportTitle": TITLES.get(row.report_id, row.report_id),
        "frequency": row.frequency,
        "recipients": row.recipients,
        "active": row.active,
        "nextRunAt": iso(row.next_run_at),
        "lastSentAt": iso(row.last_sent_at) if row.last_sent_at else None,
        "lastStatus": row.last_status,
        "createdAt": iso(row.created_at),
    }


def _clean(report_id: str, frequency: str, recipients: list[str]) -> list[str]:
    if report_id not in TITLES:
        raise AppError("Choose one of the console's reports.")
    if frequency not in ("weekly", "monthly"):
        raise AppError("Choose weekly or monthly.")
    emails = list(dict.fromkeys(r.strip().lower() for r in recipients if r.strip()))
    if not emails:
        raise AppError("Add at least one email address.")
    if len(emails) > MAX_RECIPIENTS:
        raise AppError(f"At most {MAX_RECIPIENTS} recipients per schedule.")
    bad = next((e for e in emails if not EMAIL.match(e)), None)
    if bad:
        raise AppError(f"{bad} is not a valid email address.")
    return emails


def _schedule(db: DB, schedule_id: str) -> ReportSchedule:
    row = db.get(ReportSchedule, schedule_id)
    if row is None:
        raise NotFound("Schedule not found.")
    return row


def _summary(row: ReportSchedule) -> str:
    return f"the {TITLES.get(row.report_id, row.report_id)} report ({row.frequency}, to {', '.join(row.recipients)})"


def list_schedules(db: DB) -> list[dict]:
    rows = db.scalars(select(ReportSchedule).order_by(ReportSchedule.created_at)).all()
    return [out(r) for r in rows]


def create(db: DB, actor: Admin, report_id: str, frequency: str, recipients: list[str]) -> dict:
    emails = _clean(report_id, frequency, recipients)
    now = now_utc()
    row = ReportSchedule(
        report_id=report_id, frequency=frequency, recipients=emails, active=True,
        created_by=actor.id, created_at=now, next_run_at=next_run(frequency, now),
    )
    db.add(row)
    db.flush()
    append_audit(
        db, actor, action="report.schedule_create", target_type="system", target_id=f"schedule:{row.id}",
        summary=f"Scheduled {_summary(row)}", after=out(row),
    )
    db.commit()
    return out(row)


def update(db: DB, actor: Admin, schedule_id: str, *, frequency: str | None, recipients: list[str] | None, active: bool | None) -> dict:
    row = _schedule(db, schedule_id)
    before = out(row)
    emails = _clean(row.report_id, frequency or row.frequency, recipients if recipients is not None else row.recipients)
    if frequency and frequency != row.frequency:
        row.frequency = frequency
        row.next_run_at = next_run(frequency, now_utc())
    if active is True and not row.active:
        # Back on: the next regular time, not a catch-up of the ones missed while paused.
        row.next_run_at = next_run(row.frequency, now_utc())
    row.recipients = emails
    if active is not None:
        row.active = active
    append_audit(
        db, actor, action="report.schedule_update", target_type="system", target_id=f"schedule:{row.id}",
        summary=f"Changed the schedule for {_summary(row)}", before=before, after=out(row),
    )
    db.commit()
    return out(row)


def delete(db: DB, actor: Admin, schedule_id: str) -> None:
    row = _schedule(db, schedule_id)
    append_audit(
        db, actor, action="report.schedule_delete", target_type="system", target_id=f"schedule:{row.id}",
        summary=f"Deleted the schedule for {_summary(row)}", before=out(row),
    )
    db.delete(row)
    db.commit()


# ---------- Sending ----------


def send_due(db: DB, now: datetime | None = None) -> int:
    """Sends every due schedule once; how many went out. Each schedule moves to its next time
    whether or not the email worked, and records what happened."""
    now = now or now_utc()
    sent = 0
    while True:
        row = db.scalars(
            select(ReportSchedule)
            .where(ReportSchedule.active.is_(True), ReportSchedule.next_run_at <= now)
            .order_by(ReportSchedule.next_run_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if row is None:
            return sent
        try:
            row.last_status = _send(db, row)
        except Exception as error:  # A report that can't be built is recorded, not retried every minute.
            log.warning("Scheduled report %s failed", row.id, exc_info=True)
            row.last_status = f"failed: {type(error).__name__}"
        row.last_sent_at = now
        row.next_run_at = next_run(row.frequency, now)
        db.commit()
        sent += row.last_status.startswith("sent")


def _send(db: DB, row: ReportSchedule) -> str:
    if not get_settings().email_enabled:
        return "failed: email is not configured"
    report = insights.build(db, row.report_id)
    attachment = (report_file_name(report, "csv"), "text/csv", report_to_csv(report).encode("utf-8-sig"))
    lines = [f"{s['label']}: {s['value']}" for s in report["summary"]]
    text = "\n".join([
        f"The {report['title']} report from AgriCtec, as of {report['generatedAt']:%d %b %Y %H:%M} UTC.",
        "",
        *lines,
        "",
        "The full report is attached as a CSV file.",
        f"Change or stop this email in the console: {get_settings().public_app_url.rstrip('/')}/admin/reports",
    ])
    failures = []
    for address in row.recipients:
        try:
            messaging.send_email(address, f"AgriCtec: {report['title']}", text, [attachment])
        except messaging.MessageFailed as error:
            failures.append(f"{address} ({error})")
        except Exception as error:  # Never let the mail server take the worker down.
            log.warning("Scheduled report to %s failed", address, exc_info=True)
            failures.append(f"{address} ({type(error).__name__})")
    delivered = len(row.recipients) - len(failures)
    if not failures:
        return f"sent to {delivered}"
    return (f"sent to {delivered}; failed: " if delivered else "failed: ") + "; ".join(failures)[:150]


class Scheduler:
    """The background loop (one per worker; the row locks keep sends single). It also clears
    stale rate-limit counters."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if get_settings().environment == "test" or self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agrictec-reports", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(CHECK_EVERY_SECONDS):
            try:
                with SessionLocal() as db:
                    send_due(db)
                    purge_stale(db)
                with SessionLocal() as db:
                    # Twice a day; the first worker to claim the slot does it.
                    farm_alerts.run_weather_alerts(db)
            except Exception as error:  # A bad moment (database restart) shouldn't end the loop.
                log.warning("Scheduled reports check failed", exc_info=True)
                alerts.report(f"The background jobs failed: {type(error).__name__}")


scheduler = Scheduler()
