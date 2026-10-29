"""Rounding and formatting that match the frontend exactly (lib/format.ts, Math.round)."""

import math
from datetime import date

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def js_round(value: float, digits: int = 0) -> float:
    """JavaScript's Math.round (halves go up), not Python's banker's rounding."""
    factor = 10**digits
    return math.floor(value * factor + 0.5) / factor


def round_int(value: float) -> int:
    return int(math.floor(value + 0.5))


def format_ghs(amount: float) -> str:
    """format_ghs(1234.5) -> "GHS 1,235", format_ghs(-500) -> "-GHS 500"."""
    sign = "-" if amount < 0 else ""
    # Intl rounds halves away from zero.
    return f"{sign}GHS {int(math.floor(abs(amount) + 0.5)):,}"


def format_date(value: date) -> str:
    """"18 Oct 2026"."""
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def month_label(value: date, long: bool = False, year: str = "yyyy") -> str:
    """"Oct 2026", "October 2026" or "Oct 26"."""
    name = (MONTHS_LONG if long else MONTHS)[value.month - 1]
    return f"{name} {value.year if year == 'yyyy' else str(value.year)[2:]}"


def plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def list_of(items: list[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"
