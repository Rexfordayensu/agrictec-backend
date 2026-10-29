"""Announcement life cycle and audience (lib/admin/announcements.ts)."""

from datetime import datetime
from typing import Literal

from app.schemas.platform import Announcement, AnnouncementInput

AnnouncementState = Literal["live", "scheduled", "ended", "off"]

SEVERITY = {"critical": 0, "warning": 1, "success": 2, "info": 3}


def state_of(a: AnnouncementInput, now: datetime) -> AnnouncementState:
    """Switched off, not started, live or over."""
    if not a.active:
        return "ended" if a.ends_at and a.ends_at <= now else "off"
    if a.starts_at > now:
        return "scheduled"
    if a.ends_at and a.ends_at <= now:
        return "ended"
    return "live"


def is_live(a: AnnouncementInput, now: datetime) -> bool:
    return state_of(a, now) == "live"


def can_edit(a: AnnouncementInput, now: datetime) -> bool:
    """Only before it starts: afterwards farmers have seen it (end it and duplicate instead)."""
    return state_of(a, now) == "scheduled"


def can_delete(a: AnnouncementInput, now: datetime) -> bool:
    """Only what no farmer is seeing."""
    return state_of(a, now) != "live"


def can_end(a: AnnouncementInput, now: datetime) -> bool:
    return state_of(a, now) in ("live", "scheduled")


def in_audience(audience, region: str, crops: list[str]) -> bool:
    if audience.kind == "all":
        return True
    if audience.kind == "region":
        return region in audience.regions
    return any(crop in crops for crop in audience.crops)


def needs_critical_confirm(a: AnnouncementInput) -> bool:
    """Critical, to everyone: the one combination that asks for an extra confirmation."""
    return a.type == "critical" and a.audience.kind == "all"


def by_severity(items: list[Announcement]) -> list[Announcement]:
    """Critical first, then warning, success, info; newest first within each."""
    newest = sorted(items, key=lambda a: a.starts_at, reverse=True)
    return sorted(newest, key=lambda a: SEVERITY[a.type])
