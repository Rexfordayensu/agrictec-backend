"""A farmer's financial summary for a reporting period: exact, from recorded entries only.

Pure: it takes the farmer's farm records and MoMo entries (anything with the same attributes)
and returns figures as decimal strings. Nothing is projected or annualised: when the records
cover fewer months than asked for, the summary says so (`insufficient`) and reports what exists.

- Farm income and expenses come from farm records only. MoMo and financing entries are reported
  on their own lines and never added to farm income or expenses.
- "Verified" means verified by an AgriCtec reviewer from evidence. Everything else is
  self-reported (with or without evidence, under review, rejected or disputed).
- Entries dated after today are never counted.
- Loan obligations are "as recorded by the farmer": disbursements recorded minus repayments
  recorded, over all time (a loan may predate the period). Not a confirmed debt."""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal

from app.domain.clock import add_months, month_start

ZERO = Decimal("0")
STATUSES = [
    "self_reported",
    "evidence_submitted",
    "verification_pending",
    "verified",
    "rejected",
    "disputed",
]
PERIODS = (3, 6, 12)


def money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def share(part: Decimal, whole: Decimal) -> str:
    """A percentage to 2 decimal places, "0.00" when there is nothing to divide."""
    return f"{(part / whole * 100).quantize(Decimal('0.01'))}" if whole else "0.00"


def _amount(row) -> Decimal:
    value = row.amount
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _verified(row) -> bool:
    return getattr(row, "verification_status", "self_reported") == "verified"


class _Line:
    """A total and its verified part."""

    def __init__(self) -> None:
        self.total = ZERO
        self.verified = ZERO

    def add(self, row) -> None:
        amount = _amount(row)
        self.total += amount
        if _verified(row):
            self.verified += amount

    def out(self) -> dict:
        return {
            "total": money(self.total),
            "verified": money(self.verified),
            "selfReported": money(self.total - self.verified),
            "verifiedShare": share(self.verified, self.total),
        }


def period_bounds(months: int, today: date) -> tuple[date, date]:
    """The first day of the month `months - 1` months ago, up to today (inclusive)."""
    if months not in PERIODS:
        raise ValueError(f"months must be one of {PERIODS}")
    return add_months(month_start(today), -(months - 1)), today


def _month_keys(start: date, months: int) -> list[str]:
    return [add_months(start, i).strftime("%Y-%m") for i in range(months)]


def build(farm: list, momo: list, months: int, today: date) -> dict:
    start, end = period_bounds(months, today)
    in_period = lambda row: start <= row.date <= end  # noqa: E731
    farm_now = [r for r in farm if in_period(r)]
    momo_now = [t for t in momo if in_period(t)]

    income, expenses = _Line(), _Line()
    momo_in, momo_out = _Line(), _Line()
    financing_in, financing_out = _Line(), _Line()
    monthly = {key: {"income": ZERO, "expenses": ZERO} for key in _month_keys(start, months)}
    for r in farm_now:
        key = r.date.strftime("%Y-%m")
        if r.type == "income":
            income.add(r)
            monthly[key]["income"] += _amount(r)
        elif r.type == "expense":
            expenses.add(r)
            monthly[key]["expenses"] += _amount(r)
        elif r.type == "financing":
            (financing_in if r.direction == "in" else financing_out).add(r)
    for t in momo_now:
        (momo_in if t.direction == "in" else momo_out).add(t)

    counts = Counter({status: 0 for status in STATUSES})
    total_amount = verified_amount = ZERO
    for row in [*farm_now, *momo_now]:
        # Every entry counts, farm activities (amount 0) included.
        counts[getattr(row, "verification_status", "self_reported")] += 1
        total_amount += _amount(row)
        if _verified(row):
            verified_amount += _amount(row)
    entries = sum(counts.values())

    # Obligations over all time, as recorded (never in the future).
    disbursed = repaid = interest = ZERO
    for r in farm:
        if r.type != "financing" or r.date > end:
            continue
        category = getattr(r, "category", None)
        if category == "loan_disbursement":
            disbursed += _amount(r)
        elif category == "loan_repayment":
            repaid += _amount(r)
        elif category == "interest_payment":
            interest += _amount(r)
    repayments = sorted(
        (
            r
            for r in farm_now
            if r.type == "financing" and getattr(r, "category", None) in ("loan_repayment", "interest_payment")
        ),
        key=lambda r: r.date,
        reverse=True,
    )

    active_months = {row.date.strftime("%Y-%m") for row in [*farm_now, *momo_now]}
    stamps = [row.created_at for row in [*farm, *momo] if isinstance(getattr(row, "created_at", None), datetime)]
    net = income.total - expenses.total
    return {
        "currency": "GHS",
        "period": {"from": start.isoformat(), "to": end.isoformat(), "months": months},
        "dataAsOf": max(stamps) if stamps else None,
        "farm": {"income": income.out(), "expenses": expenses.out(), "net": money(net)},
        "momo": {"in": momo_in.out(), "out": momo_out.out(), "net": money(momo_in.total - momo_out.total)},
        "financing": {"in": financing_in.out(), "out": financing_out.out()},
        "monthly": [
            {
                "month": key,
                "income": money(v["income"]),
                "expenses": money(v["expenses"]),
                "net": money(v["income"] - v["expenses"]),
            }
            for key, v in monthly.items()
        ],
        "counts": {
            "byStatus": dict(counts),
            "total": entries,
            "verified": counts["verified"],
            "farmRecords": len(farm_now),
            "financialRecords": sum(1 for r in farm_now if r.type in ("income", "expense")),
            "momo": len(momo_now),
        },
        "verifiedShare": {
            "byCount": share(Decimal(counts["verified"]), Decimal(entries)),
            "byAmount": share(verified_amount, total_amount),
        },
        "obligations": {
            "disbursed": money(disbursed),
            "repaid": money(repaid),
            "interestPaid": money(interest),
            "outstanding": money(max(ZERO, disbursed - repaid)),
        },
        "repayments": [
            {
                "date": r.date.isoformat(),
                "amount": money(_amount(r)),
                "category": r.category,
                "description": r.description,
                "verificationStatus": getattr(r, "verification_status", "self_reported"),
            }
            for r in repayments
        ],
        "monthsWithRecords": len(active_months),
        "insufficient": len(active_months) < months,
    }
