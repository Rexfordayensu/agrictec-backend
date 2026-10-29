"""Time helpers. Everything is UTC (Ghana keeps GMT all year, so UTC dates are local dates)."""

from datetime import UTC, date, datetime, timedelta


def now_utc() -> datetime:
    """The current time to the millisecond, the precision the frontend's ISO strings carry."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def iso(value: datetime) -> str:
    """"2026-10-27T09:30:00.000Z", the same shape as JavaScript's toISOString()."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def today() -> date:
    return now_utc().date()


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def month_start(value: date) -> date:
    return value.replace(day=1)


def add_months(value: date, months: int) -> date:
    """Same day `months` later (or earlier), clamped to the end of a shorter month."""
    index = value.year * 12 + value.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(value.day, last))


def months_between(later: date, earlier: date) -> int:
    """Calendar months from `earlier` to `later` (date-fns differenceInCalendarMonths)."""
    return (later.year - earlier.year) * 12 + later.month - earlier.month


def same_month(a: date, b: date) -> bool:
    return a.year == b.year and a.month == b.month


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])
