"""A loan's repayments from the lender's own published terms, in exact decimals. Pure.

Nothing is assumed: when the rate, its period, the interest method or the repayment frequency
is missing, or the method is "other", there is no schedule (`schedule` returns a reason
instead), because any figure would be invented.

Methods (as lenders state them):
- flat: interest on the full amount for the whole term (amount x monthly rate x months), the
  total split into equal instalments;
- reducing_balance: equal instalments, interest each period on what is still owed (annuity).

Frequencies: weekly (52 a year), monthly, quarterly (the term must be whole quarters),
end_of_season and lump_sum (one payment at the end of the term). Fees are listed separately
(paid on top: fixed, or a percentage of the amount), never folded into the instalments.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")
TWELVE = Decimal(12)


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Schedule:
    frequency: str
    payments: int
    instalment: Decimal
    total_repayment: Decimal  # principal + interest
    total_interest: Decimal
    total_fees: Decimal
    # The total repayment spread over the term's months: comparable with monthly cash flow
    # even for a single end-of-season payment.
    average_monthly: Decimal


def _payments(frequency: str, months: int) -> int | None:
    if frequency == "monthly":
        return months
    if frequency == "quarterly":
        return months // 3 if months % 3 == 0 else None
    if frequency == "weekly":
        return int((Decimal(months) * 52 / TWELVE).to_integral_value(rounding=ROUND_HALF_UP))
    if frequency in ("end_of_season", "lump_sum"):
        return 1
    return None


def schedule(
    *,
    amount: Decimal,
    months: int,
    rate: Decimal | None,
    rate_period: str | None,
    method: str | None,
    frequency: str | None,
    fees: list[dict] | None = None,
) -> Schedule | str:
    """The schedule, or why it can't be worked out from what the lender published."""
    if amount <= 0 or months <= 0:
        return "Enter an amount and a repayment period."
    if rate is None:
        return "The lender has not published an interest rate for this product."
    if rate_period not in ("year", "month"):
        return "The lender has not said whether the rate is per year or per month."
    if method not in ("flat", "reducing_balance"):
        return "The lender has not published how interest is calculated (flat or reducing balance)."
    if frequency is None or frequency == "other":
        return "The lender has not published how often repayments are made."
    payments = _payments(frequency, months)
    if not payments:
        return f"A {months}-month term can't be repaid in whole quarters."

    monthly_rate = rate / 100 / (TWELVE if rate_period == "year" else 1)
    if method == "flat" or monthly_rate == 0:
        total_interest = cents(amount * monthly_rate * months)
        total = amount + total_interest
        instalment = cents(total / payments)
    else:
        # Interest per repayment period, compounding the monthly rate over the period's months.
        months_per_payment = Decimal(months) / payments
        period_rate = (1 + monthly_rate) ** months_per_payment - 1
        if payments == 1:
            instalment = cents(amount * (1 + period_rate))
        else:
            instalment = cents(amount * period_rate / (1 - (1 + period_rate) ** -payments))
        total = instalment * payments
        total_interest = total - amount

    total_fees = ZERO
    for fee in fees or []:
        value = Decimal(str(fee.get("value", 0)))
        total_fees += value if fee.get("kind") == "fixed" else amount * value / 100
    return Schedule(
        frequency=frequency,
        payments=payments,
        instalment=instalment,
        total_repayment=cents(total),
        total_interest=cents(total_interest),
        total_fees=cents(total_fees),
        average_monthly=cents(total / months),
    )
