"""The MoMo Transactions page's figures: exact totals from the farmer's own MoMo entries.

Pure, like financial_summary: it takes MoMo entries (anything with direction, amount, network,
date and verification_status) and returns decimal strings. The same rules as the financial
summary apply: entries dated after today are not counted, every verification status counts
towards the totals, and "verified" means verified by an AgriCtec reviewer from evidence."""

from collections import Counter
from datetime import date
from decimal import Decimal

from app.domain.clock import add_months, month_start
from app.domain.financial_summary import STATUSES, money, share

ZERO = Decimal("0")
NETWORKS = ("MTN", "Telecel", "AirtelTigo")
TREND_MONTHS = 6


def _amount(row) -> Decimal:
    value = row.amount
    return value if isinstance(value, Decimal) else Decimal(str(value))


class _Flow:
    def __init__(self) -> None:
        self.money_in = ZERO
        self.money_out = ZERO
        self.count_in = 0
        self.count_out = 0

    def add(self, row) -> None:
        if row.direction == "in":
            self.money_in += _amount(row)
            self.count_in += 1
        else:
            self.money_out += _amount(row)
            self.count_out += 1

    def out(self) -> dict:
        return {
            "in": money(self.money_in),
            "out": money(self.money_out),
            "net": money(self.money_in - self.money_out),
            "count": self.count_in + self.count_out,
            "countIn": self.count_in,
            "countOut": self.count_out,
        }


def _change(now: Decimal, before: Decimal) -> str | None:
    """Percent change on last month, or None when last month had nothing to compare with."""
    if not before:
        return None
    return f"{((now - before) / before * 100).quantize(Decimal('0.01'))}"


def build(momo: list, today: date) -> dict:
    rows = [t for t in momo if t.date <= today]
    this_start = month_start(today)
    last_start = add_months(this_start, -1)
    trend_start = add_months(this_start, -(TREND_MONTHS - 1))

    all_time, this_month, last_month = _Flow(), _Flow(), _Flow()
    monthly = {add_months(trend_start, i).strftime("%Y-%m"): _Flow() for i in range(TREND_MONTHS)}
    by_network = {name: {"amount": ZERO, "count": 0} for name in NETWORKS}
    untagged = {"amount": ZERO, "count": 0}
    statuses: Counter = Counter()
    for t in rows:
        all_time.add(t)
        if t.date >= this_start:
            this_month.add(t)
        elif t.date >= last_start:
            last_month.add(t)
        key = t.date.strftime("%Y-%m")
        if key in monthly:
            monthly[key].add(t)
        slot = by_network.get(t.network or "", untagged)
        slot["amount"] += _amount(t)
        slot["count"] += 1
        statuses[getattr(t, "verification_status", None) or "self_reported"] += 1

    moved = all_time.money_in + all_time.money_out
    total = len(rows)
    networks = [
        {
            "network": name,
            "amount": money(slot["amount"]),
            "count": slot["count"],
            "amountShare": share(slot["amount"], moved),
            "countShare": share(Decimal(slot["count"]), Decimal(total)),
        }
        for name, slot in by_network.items()
    ]
    top = max(networks, key=lambda n: (n["count"], Decimal(n["amount"])))
    verified = statuses["verified"]
    return {
        "currency": "GHS",
        "asOf": today.isoformat(),
        "allTime": {
            **all_time.out(),
            "moved": money(moved),
            "verified": verified,
            "verifiedShare": share(Decimal(verified), Decimal(total)),
        },
        "thisMonth": {"month": this_start.strftime("%Y-%m"), **this_month.out()},
        "lastMonth": {"month": last_start.strftime("%Y-%m"), **last_month.out()},
        "changeOnLastMonth": {
            "in": _change(this_month.money_in, last_month.money_in),
            "out": _change(this_month.money_out, last_month.money_out),
        },
        "monthly": [{"month": key, **flow.out()} for key, flow in monthly.items()],
        "networks": networks,
        "untagged": {"amount": money(untagged["amount"]), "count": untagged["count"]},
        # The network with the most transactions; none while nothing is tagged.
        "mainNetwork": None if top["count"] == 0 else {"network": top["network"], "countShare": top["countShare"]},
        "byStatus": {status: statuses.get(status, 0) for status in STATUSES},
    }
