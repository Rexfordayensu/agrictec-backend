"""Farm finance (lib/calculations/finance.ts): the parts the server needs."""

from collections.abc import Iterable
from datetime import date

from app.domain.clock import add_months, months_between, today
from app.domain.fmt import js_round, round_int
from app.schemas.farm import FarmRecord


def _income(r: FarmRecord) -> float:
    return r.amount if r.type == "income" else 0


def _expense(r: FarmRecord) -> float:
    return r.amount if r.type == "expense" else 0


def total_income(records: Iterable[FarmRecord]) -> float:
    return sum(_income(r) for r in records)


def total_expenses(records: Iterable[FarmRecord]) -> float:
    return sum(_expense(r) for r in records)


def net_profit(records: list[FarmRecord]) -> float:
    return total_income(records) - total_expenses(records)


def profit_margin(records: list[FarmRecord]) -> float:
    """(profit / income) x 100 to 1 decimal place, or 0 when there is no income."""
    income = total_income(records)
    if income <= 0:
        return 0
    return js_round(net_profit(records) / income * 100, 1)


def annualized_net_profit(records: list[FarmRecord], now: date | None = None) -> int:
    """Net profit over the last 12 months, scaled up to a full year when the data covers fewer
    months: profit / months spanned x 12."""
    now = now or today()
    cutoff = add_months(now, -12)
    recent = [r for r in records if r.date > cutoff]
    if not recent:
        return 0
    earliest = min(r.date for r in recent)
    months = min(12, max(1, months_between(now, earliest) + 1))
    return round_int(net_profit(recent) / months * 12)
