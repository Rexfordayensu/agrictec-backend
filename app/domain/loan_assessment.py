"""Transaction-based loan eligibility: what a farmer's recorded history shows, checked against one
loan product's published terms and rules. Pure: no database (app/services/loan_eligibility.py
loads the data and keeps a snapshot of each result).

Principles:
- Only recorded entries count, never anything simulated or projected. Rejected, disputed and
  duplicate-flagged entries are left out, and so are future-dated ones.
- Money received is not all income. Farm income (sales) is income; borrowed money (loan
  disbursements) never is; loan repayments and interest are existing debt service, not costs.
  Mobile Money entries are part of the history: one categorised as a farm sale or cost counts
  as one, an uncategorised one is money in or out but not income or a cost, and a MoMo entry
  that matches a farm record (same date, amount and direction) is the same transaction, counted
  once.
- A lender rule is applied only as the administrator recorded it, with its source; a rule past
  its review date is not applied. A missing threshold is never filled in.
- Short or incomplete history asks for more information; it never fails the farmer by itself.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.domain.clock import add_months, month_start
from app.domain.repayment import Schedule, cents, schedule
from app.domain.transactions import CATEGORIES

ZERO = Decimal("0")
HUNDRED = Decimal("100")
DEFAULT_PERIOD_MONTHS = 12
EXCLUDED_STATUSES = {"rejected", "disputed"}

INCOME_CATEGORIES = set(CATEGORIES["income"])
EXPENSE_CATEGORIES = set(CATEGORIES["expense"])
DEBT_SERVICE = {"loan_repayment", "interest_payment"}

OUTCOMES = ("meets_preliminary", "additional_information", "does_not_meet", "unable_to_assess")
OUTCOME_LABELS = {
    "meets_preliminary": "Meets preliminary requirements",
    "additional_information": "Additional information required",
    "does_not_meet": "Does not meet stated requirements",
    "unable_to_assess": "Unable to assess",
}

# Metrics whose shortfall means "record more" (the farmer can fix it) rather than "not met".
HISTORY_METRICS = {"months_of_records", "record_count", "history_months", "verified_share"}
# Metrics about a fact the farmer can fix in AgriCtec (verification, profile).
FIXABLE_FACTS = {"ghana_card_verified", "phone_verified", "location_verified", "profile_complete"}


# ---------- Transaction analysis ----------


@dataclass
class Entry:
    kind: str  # "farm" | "momo"
    id: str
    on: date
    recorded_at: datetime | None
    flow: str  # income | expense | borrowed | debt_service | other_in | other_out | activity
    amount: Decimal
    verified: bool
    category: str | None
    matched: bool = False  # a MoMo entry already counted as a farm record


def _farm_flow(row) -> str:
    if row.type == "income":
        return "income"
    if row.type == "expense":
        return "expense"
    if row.type == "financing":
        if (row.category or "") in DEBT_SERVICE:
            return "debt_service"
        return "borrowed" if row.direction == "in" else "other_out"
    return "activity"


def _momo_flow(row) -> str:
    category = row.category or ""
    if row.direction == "in":
        if category in INCOME_CATEGORIES:
            return "income"
        if category == "loan_disbursement" or category == "other_financing":
            return "borrowed"
        return "other_in"
    if category in EXPENSE_CATEGORIES:
        return "expense"
    if category in DEBT_SERVICE:
        return "debt_service"
    return "other_out"


def entries_from(farm: list, momo: list, today: date) -> list[Entry]:
    """Every countable entry, with MoMo copies of farm records marked as matched."""
    out: list[Entry] = []
    for r in farm:
        if r.date > today or getattr(r, "verification_status", "") in EXCLUDED_STATUSES:
            continue
        out.append(Entry("farm", r.id, r.date, getattr(r, "created_at", None), _farm_flow(r),
                         Decimal(r.amount or 0), getattr(r, "verification_status", "") == "verified",
                         getattr(r, "category", None)))
    farm_keys = defaultdict(int)
    for e in out:
        if e.flow in ("income", "expense", "borrowed", "debt_service"):
            farm_keys[(e.on, e.amount, e.flow)] += 1
    for t in momo:
        if t.date > today or getattr(t, "verification_status", "") in EXCLUDED_STATUSES:
            continue
        if getattr(t, "reference_duplicate", False):
            continue  # the same MoMo reference twice: the first one stands
        entry = Entry("momo", t.id, t.date, getattr(t, "created_at", None), _momo_flow(t),
                      Decimal(t.amount or 0), getattr(t, "verification_status", "") == "verified",
                      getattr(t, "category", None))
        key = (entry.on, entry.amount, entry.flow)
        if farm_keys.get(key):
            farm_keys[key] -= 1
            entry.matched = True
        out.append(entry)
    return out


@dataclass
class Analysis:
    period_months: int
    period_from: date
    period_to: date
    income: Decimal = ZERO
    expenses: Decimal = ZERO
    borrowed: Decimal = ZERO
    debt_service: Decimal = ZERO
    other_in: Decimal = ZERO
    other_out: Decimal = ZERO
    entries: int = 0
    farm_records: int = 0
    momo_entries: int = 0
    matched_momo: int = 0
    verified_entries: int = 0
    verified_amount: Decimal = ZERO
    counted_amount: Decimal = ZERO
    months_with_records: int = 0
    months_with_income: int = 0
    history_months: int = 0
    first_entry: date | None = None
    largest_income: Decimal = ZERO
    disbursed_all_time: Decimal = ZERO
    repaid_all_time: Decimal = ZERO
    monthly: dict[str, dict[str, Decimal]] = field(default_factory=dict)

    @property
    def net(self) -> Decimal:
        return self.income - self.expenses

    def per_month(self, value: Decimal) -> Decimal:
        return cents(value / self.period_months)

    @property
    def avg_monthly_net(self) -> Decimal:
        return self.per_month(self.net)

    @property
    def avg_monthly_income(self) -> Decimal:
        return self.per_month(self.income)

    @property
    def existing_monthly_debt_service(self) -> Decimal:
        return self.per_month(self.debt_service)

    @property
    def outstanding(self) -> Decimal:
        return max(ZERO, self.disbursed_all_time - self.repaid_all_time)

    @property
    def verified_share(self) -> Decimal | None:
        return cents(self.verified_amount / self.counted_amount * HUNDRED) if self.counted_amount else None

    @property
    def income_months_share(self) -> Decimal:
        return cents(Decimal(self.months_with_income) / self.period_months * HUNDRED)

    @property
    def debt_burden(self) -> Decimal | None:
        income = self.avg_monthly_income
        return cents(self.existing_monthly_debt_service / income * HUNDRED) if income > 0 else None

    @property
    def has_money_records(self) -> bool:
        return self.income > 0 or self.expenses > 0


def analyse(entries: list[Entry], *, months: int, today: date) -> Analysis:
    start = add_months(month_start(today), -(months - 1))
    a = Analysis(period_months=months, period_from=start, period_to=today)
    keys = [add_months(start, i).strftime("%Y-%m") for i in range(months)]
    a.monthly = {k: {"income": ZERO, "expenses": ZERO} for k in keys}
    active, with_income = set(), set()
    for e in entries:
        if e.flow == "borrowed" and (e.category == "loan_disbursement" or e.kind == "farm") and not e.matched:
            a.disbursed_all_time += e.amount
        if e.flow == "debt_service" and e.category == "loan_repayment" and not e.matched:
            a.repaid_all_time += e.amount
        if not e.matched:
            a.first_entry = e.on if a.first_entry is None or e.on < a.first_entry else a.first_entry
        if not (start <= e.on <= today):
            continue
        month = e.on.strftime("%Y-%m")
        active.add(month)
        a.entries += 1
        a.farm_records += e.kind == "farm"
        a.momo_entries += e.kind == "momo"
        if e.matched:
            a.matched_momo += 1
            continue  # counted once, as the farm record
        if e.verified:
            a.verified_entries += 1
        if e.flow in ("income", "expense"):
            a.counted_amount += e.amount
            if e.verified:
                a.verified_amount += e.amount
        if e.flow == "income":
            a.income += e.amount
            a.monthly[month]["income"] += e.amount
            a.largest_income = max(a.largest_income, e.amount)
            if e.amount > 0:
                with_income.add(month)
        elif e.flow == "expense":
            a.expenses += e.amount
            a.monthly[month]["expenses"] += e.amount
        elif e.flow == "borrowed":
            a.borrowed += e.amount
        elif e.flow == "debt_service":
            a.debt_service += e.amount
        elif e.flow == "other_in":
            a.other_in += e.amount
        elif e.flow == "other_out":
            a.other_out += e.amount
    a.months_with_records = len(active)
    a.months_with_income = len(with_income)
    if a.first_entry:
        first = month_start(a.first_entry)
        a.history_months = (today.year - first.year) * 12 + today.month - first.month + 1
    return a


# ---------- Rules ----------

METRIC_LABELS = {
    "ghana_card_verified": "Ghana Card",
    "phone_verified": "Phone number verified",
    "location_verified": "Farm location verified",
    "profile_complete": "Farm profile complete",
    "cooperative_member": "Member of a cooperative",
    "farm_size_acres": "Farm size (acres)",
    "farm_region": "Farm region",
    "primary_crop": "Main crop",
    "farm_type": "Farm type",
    "months_of_records": "Months with records in the assessment period",
    "record_count": "Farm records in the assessment period",
    "history_months": "Months since your first recorded entry",
    "income_12m": "Recorded income, last 12 months",
    "net_12m": "Recorded net result, last 12 months",
    "period_income": "Documented income in the assessment period",
    "period_net": "Net cash flow in the assessment period",
    "avg_monthly_net": "Average monthly net cash flow",
    "verified_share": "Verified share of recorded income and costs",
    "income_months_share": "Share of months with income",
    "debt_burden": "Existing loan repayments as a share of income",
    "dscr": "Debt-service coverage ratio",
}
MONEY_METRICS = {"income_12m", "net_12m", "period_income", "period_net", "avg_monthly_net"}
PERCENT_METRICS = {"verified_share", "income_months_share", "debt_burden"}


def fmt_value(metric: str, value: Any) -> str:
    if value is None:
        return "not available"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if metric in MONEY_METRICS:
        return f"GHS {Decimal(value):,.2f}"
    if metric in PERCENT_METRICS:
        return f"{Decimal(value):.2f}%"
    if metric == "dscr":
        return f"{Decimal(value):.2f}"
    if isinstance(value, list):
        return ", ".join(value) or "none"
    return str(value)


def fmt_threshold(metric: str, op: str, value: Any) -> str:
    if op == "is_true":
        return "required"
    if op == "in":
        return "one of: " + ", ".join(value or [])
    word = "at least" if op == "gte" else "at most"
    return f"{word} {fmt_value(metric, value)}"


@dataclass
class Facts:
    """What AgriCtec holds about the farmer (besides transactions)."""

    identity_level: str  # none | reviewed | nia_verified
    phone_verified: bool
    location_verified: bool
    profile_complete: bool
    cooperative_member: bool
    acres: Decimal | None
    region: str | None
    crops: list[str]
    farm_type: str | None


def _metric_value(metric: str, facts: Facts, a: Analysis, a12: Analysis, plan: Schedule | str) -> tuple[Any, str | None]:
    """(value, why it is unavailable)."""
    money_needed = metric in {"period_income", "period_net", "avg_monthly_net", "income_months_share",
                              "debt_burden", "dscr", "income_12m", "net_12m"}
    data = a12 if metric in ("income_12m", "net_12m") else a
    if money_needed and not data.has_money_records:
        return None, "no_records"
    match metric:
        case "ghana_card_verified":
            return facts.identity_level in ("reviewed", "nia_verified"), None
        case "phone_verified":
            return facts.phone_verified, None
        case "location_verified":
            return facts.location_verified, None
        case "profile_complete":
            return facts.profile_complete, None
        case "cooperative_member":
            return facts.cooperative_member, None
        case "farm_size_acres":
            return facts.acres, None if facts.acres is not None else "no_profile"
        case "farm_region":
            return facts.region, None if facts.region else "no_profile"
        case "primary_crop":
            return facts.crops, None
        case "farm_type":
            return facts.farm_type, None if facts.farm_type else "no_profile"
        case "months_of_records":
            return a.months_with_records, None
        case "record_count":
            return a.farm_records, None
        case "history_months":
            return a.history_months, None
        case "income_12m":
            return a12.income, None
        case "net_12m":
            return a12.net, None
        case "period_income":
            return a.income, None
        case "period_net":
            return a.net, None
        case "avg_monthly_net":
            return a.avg_monthly_net, None
        case "verified_share":
            return (a.verified_share, None) if a.verified_share is not None else (None, "no_records")
        case "income_months_share":
            return a.income_months_share, None
        case "debt_burden":
            return (a.debt_burden, None) if a.debt_burden is not None else (None, "no_income")
        case "dscr":
            if isinstance(plan, str):
                return None, "no_schedule"
            service = a.existing_monthly_debt_service + plan.average_monthly
            return (cents(a.avg_monthly_net / service), None) if service > 0 else (None, "no_schedule")
    return None, "unknown_metric"


def _passes(op: str, value: Any, threshold: Any) -> bool:
    if op == "is_true":
        return bool(value)
    if op == "in":
        wanted = set(threshold or [])
        return bool(wanted & set(value)) if isinstance(value, list) else value in wanted
    if op == "gte":
        return Decimal(value) >= Decimal(str(threshold))
    return Decimal(value) <= Decimal(str(threshold))


UNAVAILABLE_TEXT = {
    "no_records": "There are no recorded income or expense entries in the assessment period yet.",
    "no_income": "There is no recorded income in the assessment period to compare repayments with.",
    "no_schedule": "The repayment can't be worked out from the lender's published terms.",
    "no_profile": "This detail is missing from your farm profile.",
    "unknown_metric": "AgriCtec can't check this condition.",
}
FARMER_SIDE = {"no_records", "no_income", "no_profile"}


def _fix_hint(metric: str, facts: Facts) -> str:
    return {
        "ghana_card_verified": "Add your Ghana Card for review (Settings → Verification)."
        if facts.identity_level == "none" else "Wait for your Ghana Card review to finish.",
        "phone_verified": "Verify your phone number (Settings → Verification).",
        "location_verified": "Set your farm location (Settings → Verification).",
        "profile_complete": "Complete your farm profile.",
        "months_of_records": "Keep recording your farm entries each month.",
        "record_count": "Record your farm sales and costs as they happen.",
        "history_months": "Keep recording: your history grows every month.",
        "verified_share": "Attach receipts or statements to your entries and request a review.",
    }.get(metric, "Record your income and expenses with their real dates and amounts.")


def evaluate(
    *,
    product: dict,
    lender_name: str,
    amount: Decimal,
    months: int,
    facts: Facts,
    farm: list,
    momo: list,
    today: date,
) -> dict:
    """The explainable preliminary result for one product (`product`: a ProductVersion dump,
    snake_case keys). Never a loan decision."""
    terms = product.get("terms") or {}
    period = terms.get("assessment_months") or DEFAULT_PERIOD_MONTHS
    entries = entries_from(farm, momo, today)
    a = analyse(entries, months=period, today=today)
    a12 = a if period == 12 else analyse(entries, months=12, today=today)
    plan = schedule(
        amount=amount, months=months, rate=_dec(terms.get("interest_rate")), rate_period=terms.get("rate_period"),
        method=terms.get("rate_method"), frequency=terms.get("repayment_frequency"), fees=terms.get("fees") or [],
    )

    results: list[dict] = []

    # The lender's published amount and term limits are confirmed terms.
    lo, hi = _dec(terms.get("min_amount")), _dec(terms.get("max_amount"))
    if lo is not None or hi is not None:
        inside = (lo is None or amount >= lo) and (hi is None or amount <= hi)
        results.append(_term_result("amount", "Loan amount", f"GHS {amount:,.2f}",
                                    _range_text(lo, hi, money=True), inside))
    lo_m, hi_m = terms.get("min_months"), terms.get("max_months")
    if lo_m is not None or hi_m is not None:
        inside = (lo_m is None or months >= lo_m) and (hi_m is None or months <= hi_m)
        results.append(_term_result("months", "Repayment period", f"{months} months",
                                    _range_text(lo_m, hi_m, unit="months"), inside))

    to_provide: list[dict] = []
    checks = 0
    uses_affordability = False
    for req in product.get("requirements") or []:
        check = req.get("check")
        if not check:
            if req.get("field_type", "none") != "none":
                to_provide.append({
                    "key": req["key"], "name": req["name"], "category": req["category"],
                    "required": req.get("required", True), "fieldType": req.get("field_type"),
                    "conditional": req.get("visible_if") is not None,
                    "verification": req.get("verification"),
                })
            continue
        checks += 1
        metric, op, threshold = check["metric"], check["op"], check.get("value")
        uses_affordability |= metric in ("dscr", "debt_burden")
        base = {
            "key": req["key"], "name": req["name"], "category": req["category"],
            "required": req.get("required", True), "metric": metric,
            "label": METRIC_LABELS.get(metric, metric),
            "threshold": fmt_threshold(metric, op, threshold),
            "source": req.get("source"), "effectiveFrom": _iso(req.get("effective_from")),
            "reviewedOn": _iso(req.get("reviewed_on")), "reviewBy": _iso(req.get("review_by")),
        }
        review_by, effective = req.get("review_by"), req.get("effective_from")
        if review_by is not None and _as_date(review_by) < today:
            results.append({**base, "status": "expired", "value": None,
                            "explanation": f"This rule was due for review on {_as_date(review_by):%d %b %Y}. "
                                           "It isn't applied until AgriCtec confirms it with the lender."})
            continue
        if effective is not None and _as_date(effective) > today:
            results.append({**base, "status": "not_yet_in_effect", "value": None,
                            "explanation": f"This rule applies from {_as_date(effective):%d %b %Y}."})
            continue
        value, missing = _metric_value(metric, facts, a, a12, plan)
        if missing:
            status = "needs_info" if missing in FARMER_SIDE else "unknown"
            text = UNAVAILABLE_TEXT[missing]
            if status == "needs_info":
                text += " " + _fix_hint(metric, facts)
            results.append({**base, "status": status, "value": None, "explanation": text})
            continue
        ok = _passes(op, value, threshold)
        shown = fmt_value(metric, value)
        if op == "is_true":
            # A yes/no condition: "Ghana Card: yes." or "Ghana Card: not yet. <how to fix>".
            status = "met" if ok else ("needs_info" if metric in FIXABLE_FACTS else "not_met")
            text = f"{base['label']}: yes." if ok else f"{base['label']}: not yet."
            if not ok and metric in FIXABLE_FACTS:
                text += " " + _fix_hint(metric, facts)
        elif ok:
            status, text = "met", f"{base['label']}: {shown} ({base['threshold']})."
        elif metric in HISTORY_METRICS or metric in FIXABLE_FACTS:
            status = "needs_info"
            text = f"{base['label']}: {shown}, but the lender asks for {base['threshold']}. {_fix_hint(metric, facts)}"
        else:
            status = "not_met"
            text = f"{base['label']}: {shown}, but the lender asks for {base['threshold']}."
        if metric == "dscr" and not isinstance(plan, str):
            text += (f" Calculated as average monthly net cash flow ({fmt_value('avg_monthly_net', a.avg_monthly_net)})"
                     f" ÷ (existing monthly loan repayments GHS {a.existing_monthly_debt_service:,.2f}"
                     f" + this loan's average monthly cost GHS {plan.average_monthly:,.2f}).")
        if metric == "debt_burden":
            text += (f" Calculated as existing monthly loan repayments (GHS {a.existing_monthly_debt_service:,.2f})"
                     f" ÷ average monthly income ({fmt_value('avg_monthly_net', a.avg_monthly_income)}).")
        if metric == "ghana_card_verified" and value and facts.identity_level == "reviewed":
            text += " Reviewed by AgriCtec (not an NIA identity check)."
        results.append({**base, "status": status, "value": shown, "explanation": text})

    outcome = _outcome(results, checks, plan)
    affordability = _affordability(a, plan, lender_name, ruled=uses_affordability)
    return {
        "outcome": outcome,
        "outcomeLabel": OUTCOME_LABELS[outcome],
        "summary": _summary(outcome, results, lender_name, checks),
        "request": {"amount": f"{cents(amount)}", "months": months},
        "period": {
            "months": period, "from": a.period_from.isoformat(), "to": a.period_to.isoformat(),
            "source": "lender" if terms.get("assessment_months") else "default",
        },
        "figures": _figures(a),
        "repayment": _repayment(plan),
        "affordability": affordability,
        "rules": results,
        "toProvide": to_provide,
        "notes": _notes(a),
        "disclaimer": "A preliminary check of what AgriCtec can see. It is not a loan decision: "
                      f"{lender_name} makes the final decision.",
    }


def _outcome(results: list[dict], checks: int, plan: Schedule | str) -> str:
    required = [r for r in results if r.get("required", True)]
    if any(r["status"] == "not_met" for r in required):
        return "does_not_meet"
    if any(r["status"] == "needs_info" for r in required):
        return "additional_information"
    if any(r["status"] in ("unknown", "expired") for r in required):
        return "unable_to_assess"
    if checks == 0 and not any(r["key"] in ("amount", "months") for r in results):
        return "unable_to_assess"  # the lender published nothing AgriCtec can check
    return "meets_preliminary"


def _summary(outcome: str, results: list[dict], lender: str, checks: int) -> str:
    first = next((r for r in results if r.get("required", True) and r["status"] in {
        "does_not_meet": ("not_met",), "additional_information": ("needs_info",),
        "unable_to_assess": ("unknown", "expired"), "meets_preliminary": ("met",),
    }[outcome]), None)
    if outcome == "does_not_meet" and first:
        return f"A stated requirement of {lender} is not met: {first['explanation']}"
    if outcome == "additional_information" and first:
        return f"More information is needed before this can be assessed. {first['explanation']}"
    if outcome == "unable_to_assess":
        if checks == 0 and not first:
            return f"{lender} has not published conditions AgriCtec can check. Contact the lender for its requirements."
        return f"AgriCtec can't assess everything {lender} asks for. {first['explanation'] if first else ''}".strip()
    return (f"You meet the preliminary requirements AgriCtec can check for this product. "
            f"This is not an approval: {lender} makes the final decision.")


def _figures(a: Analysis) -> dict:
    return {
        "income": f"{a.income:.2f}", "expenses": f"{a.expenses:.2f}", "net": f"{a.net:.2f}",
        "averageMonthlyNet": f"{a.avg_monthly_net:.2f}",
        "borrowed": f"{a.borrowed:.2f}", "otherMoneyIn": f"{a.other_in:.2f}", "otherMoneyOut": f"{a.other_out:.2f}",
        "existingMonthlyDebtService": f"{a.existing_monthly_debt_service:.2f}",
        "outstandingRecordedLoans": f"{a.outstanding:.2f}",
        "entries": a.entries, "farmRecords": a.farm_records, "momoEntries": a.momo_entries,
        "matchedMomo": a.matched_momo, "verifiedEntries": a.verified_entries,
        "verifiedShare": None if a.verified_share is None else f"{a.verified_share:.2f}",
        "monthsWithRecords": a.months_with_records, "monthsWithIncome": a.months_with_income,
        "historyMonths": a.history_months,
        "firstEntry": a.first_entry.isoformat() if a.first_entry else None,
        "monthly": [{"month": k, "income": f"{v['income']:.2f}", "expenses": f"{v['expenses']:.2f}"}
                    for k, v in a.monthly.items()],
    }


def _repayment(plan: Schedule | str) -> dict:
    if isinstance(plan, str):
        return {"available": False, "reason": plan}
    return {
        "available": True, "frequency": plan.frequency, "payments": plan.payments,
        "instalment": f"{plan.instalment:.2f}", "totalRepayment": f"{plan.total_repayment:.2f}",
        "totalInterest": f"{plan.total_interest:.2f}", "totalFees": f"{plan.total_fees:.2f}",
        "averageMonthly": f"{plan.average_monthly:.2f}",
    }


def _affordability(a: Analysis, plan: Schedule | str, lender: str, *, ruled: bool) -> dict:
    """Without a lender rule: the comparison, for information only, never a verdict."""
    if isinstance(plan, str) or not a.has_money_records:
        return {"available": False, "ruled": ruled,
                "reason": plan if isinstance(plan, str) else UNAVAILABLE_TEXT["no_records"]}
    left = a.avg_monthly_net - a.existing_monthly_debt_service - plan.average_monthly
    return {
        "available": True, "ruled": ruled,
        "averageMonthlyNet": f"{a.avg_monthly_net:.2f}",
        "existingMonthlyDebtService": f"{a.existing_monthly_debt_service:.2f}",
        "proposedAverageMonthly": f"{plan.average_monthly:.2f}",
        "leftAfterRepayments": f"{cents(left):.2f}",
        "note": None if ruled else f"Information only: {lender} has not published an affordability rule, "
                                    "so this comparison does not pass or fail you.",
    }


def _notes(a: Analysis) -> list[str]:
    notes = []
    if a.months_with_income and a.months_with_income < a.period_months / 2:
        notes.append(f"Income was recorded in {a.months_with_income} of {a.period_months} months: "
                     "seasonal income is normal for farming, and averages spread it across the period.")
    if a.income > 0 and a.largest_income / a.income > Decimal("0.5"):
        notes.append("More than half of the income comes from one entry. Lenders may ask about large, "
                     "irregular amounts; keep its receipt.")
    if a.matched_momo:
        notes.append(f"{a.matched_momo} MoMo {'entry matches' if a.matched_momo == 1 else 'entries match'} "
                     "a farm record with the same date and amount, and is counted once.")
    if a.borrowed > 0:
        notes.append("Borrowed money is shown separately and never counted as income.")
    if a.other_in > 0:
        notes.append("MoMo money received without a farm income category is not counted as income.")
    return notes


def _term_result(key: str, name: str, value: str, allowed: str, inside: bool) -> dict:
    return {
        "key": key, "name": name, "category": "terms", "required": True, "metric": None, "label": name,
        "threshold": allowed, "source": None, "effectiveFrom": None, "reviewedOn": None, "reviewBy": None,
        "status": "met" if inside else "not_met", "value": value,
        "explanation": f"{name}: {value} ({allowed})." if inside
        else f"{name}: {value} is outside the lender's published range ({allowed}).",
    }


def _range_text(lo, hi, *, money: bool = False, unit: str = "") -> str:
    show = (lambda v: f"GHS {Decimal(v):,.2f}") if money else (lambda v: f"{v} {unit}")
    if lo is not None and hi is not None:
        return f"{show(lo)} to {show(hi)}"
    return f"at least {show(lo)}" if lo is not None else f"at most {show(hi)}"


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _iso(value) -> str | None:
    return None if value is None else _as_date(value).isoformat()
