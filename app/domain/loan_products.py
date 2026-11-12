"""Loan products and their requirements: what a lender officially offers, and what it asks of a
farmer. Pure rules, no database.

Everything comes from the lender's own published information, typed in by an administrator,
with its source. Nothing is assumed: a term the lender didn't state stays empty ("Not
provided"), and no requirement is ever added for every lender by default.

A requirement may carry a `check`: a condition on information AgriCtec already holds (the
Ghana Card, farm size, months of records...). The readiness checker evaluates those later;
everything else the farmer provides (an answer or a document) when preparing an application."""

from datetime import date
from decimal import Decimal

from app.domain.constants import CROPS, REGIONS

FARM_TYPES = ["Smallholder", "Commercial", "Cooperative", "Out-grower"]

# What a requirement's check may look at. kind: "bool" (is_true), "number"/"amount" (gte, lte),
# "choice" (in). Only facts AgriCtec holds about the farmer; nothing guessed.
METRICS: dict[str, dict] = {
    "ghana_card_verified": {"label": "Ghana Card verified", "kind": "bool"},
    "phone_verified": {"label": "Phone number verified", "kind": "bool"},
    "location_verified": {"label": "Farm location verified", "kind": "bool"},
    "profile_complete": {"label": "Farm profile complete", "kind": "bool"},
    "cooperative_member": {"label": "Member of a cooperative", "kind": "bool"},
    "farm_size_acres": {"label": "Farm size (acres)", "kind": "number"},
    "farm_region": {"label": "Farm region", "kind": "choice", "choices": REGIONS},
    "primary_crop": {"label": "Main crop", "kind": "choice", "choices": CROPS},
    "farm_type": {"label": "Farm type", "kind": "choice", "choices": FARM_TYPES},
    "months_of_records": {"label": "Months with farm records", "kind": "number"},
    "record_count": {"label": "Number of farm records", "kind": "number"},
    "income_12m": {"label": "Recorded income, last 12 months (GHS)", "kind": "amount"},
    "net_12m": {"label": "Recorded net result, last 12 months (GHS)", "kind": "amount"},
    # Over the product's assessment period (app/domain/loan_assessment.py).
    "history_months": {"label": "Months since the first recorded entry", "kind": "number"},
    "period_income": {"label": "Documented income in the assessment period (GHS)", "kind": "amount"},
    "period_net": {"label": "Net cash flow in the assessment period (GHS)", "kind": "amount"},
    "avg_monthly_net": {"label": "Average monthly net cash flow (GHS)", "kind": "amount"},
    "verified_share": {"label": "Verified share of recorded income and costs (%)", "kind": "percent"},
    "income_months_share": {"label": "Share of months with recorded income (%)", "kind": "percent"},
    "debt_burden": {"label": "Existing loan repayments as a share of income (%)", "kind": "percent"},
    "dscr": {
        "label": "Debt-service coverage ratio: average monthly net cash flow ÷ (existing monthly "
                 "loan repayments + this loan's average monthly cost)",
        "kind": "ratio",
    },
}

# Rules about the farmer's finances: each needs the date it took effect and the date it was
# last confirmed with the lender before it can be published.
FINANCIAL_METRICS = {
    "months_of_records", "record_count", "income_12m", "net_12m", "history_months", "period_income",
    "period_net", "avg_monthly_net", "verified_share", "income_months_share", "debt_burden", "dscr",
}

OPERATORS_BY_KIND = {
    "bool": {"is_true"},
    "number": {"gte", "lte"},
    "amount": {"gte", "lte"},
    "percent": {"gte", "lte"},
    "ratio": {"gte", "lte"},
    "choice": {"in"},
}

FIELD_TYPES = ["none", "yes_no", "text", "number", "amount", "date", "select", "document"]
DOCUMENT_FORMATS = ["pdf", "jpg", "png", "webp"]


def check_problem(metric: str, op: str, value) -> str | None:
    """Why a requirement's check is not valid (None: it is)."""
    spec = METRICS.get(metric)
    if spec is None:
        return f"“{metric}” is not something AgriCtec can check."
    if op not in OPERATORS_BY_KIND[spec["kind"]]:
        return f"{spec['label']} can't be checked with “{op}”."
    if spec["kind"] in ("number", "amount", "percent", "ratio"):
        if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool) or value < 0:
            return f"{spec['label']}: enter a number of 0 or more."
        if spec["kind"] == "percent" and value > 100:
            return f"{spec['label']}: enter a percentage up to 100."
    if spec["kind"] == "choice":
        if not isinstance(value, list) or not value:
            return f"{spec['label']}: choose at least one."
        unknown = [v for v in value if v not in spec["choices"]]
        if unknown:
            return f"{spec['label']}: “{unknown[0]}” is not a valid choice."
    return None


def publish_problems(content, today: date) -> list[str]:
    """Everything that must be fixed before a product version can be published. `content` is a
    ProductContent (app/schemas/products.py); a draft may be saved with problems, not published."""
    problems: list[str] = []
    terms = content.terms
    if not (content.source or "").strip():
        problems.append("Say where these terms come from (the lender's official document or page).")
    if content.source_checked_on is None:
        problems.append("Enter the date the terms were checked against the source.")
    elif content.source_checked_on > today:
        problems.append("The date the terms were checked can't be in the future.")
    if terms.min_amount is not None and terms.max_amount is not None and terms.min_amount > terms.max_amount:
        problems.append("The smallest loan is larger than the largest loan.")
    if terms.min_months is not None and terms.max_months is not None and terms.min_months > terms.max_months:
        problems.append("The shortest period is longer than the longest period.")
    if terms.interest_rate is not None and terms.rate_period is None:
        problems.append("Say whether the interest rate is per year or per month.")
    if terms.supported_regions is not None and not terms.supported_regions:
        problems.append("Choose the regions served, or mark them as not provided.")

    keys: dict[str, object] = {}
    for position, req in enumerate(content.requirements, start=1):
        where = f"Requirement {position} ({req.name})"
        if req.key in keys:
            problems.append(f"{where}: another requirement already uses the key “{req.key}”.")
        if req.field_type == "select" and len(req.options) < 2:
            problems.append(f"{where}: a choice needs at least two options.")
        if req.field_type == "document" and not req.allowed_formats:
            problems.append(f"{where}: say which file types are accepted.")
        if req.min is not None and req.max is not None and req.min > req.max:
            problems.append(f"{where}: the minimum is larger than the maximum.")
        if req.field_type == "none" and req.check is None:
            problems.append(f"{where}: give it a check AgriCtec can make, or something the farmer provides.")
        if req.check is not None:
            issue = check_problem(req.check.metric, req.check.op, req.check.value)
            if issue:
                problems.append(f"{where}: {issue}")
            if req.check.metric in FINANCIAL_METRICS:
                if req.effective_from is None:
                    problems.append(f"{where}: enter the date this lender rule took effect.")
                if req.reviewed_on is None:
                    problems.append(f"{where}: enter the date this rule was last confirmed with the lender.")
        if req.reviewed_on is not None and req.reviewed_on > today:
            problems.append(f"{where}: the review date can't be in the future.")
        if req.reviewed_on is not None and req.review_by is not None and req.review_by < req.reviewed_on:
            problems.append(f"{where}: the next review date is before the last review.")
        if req.visible_if is not None:
            parent = keys.get(req.visible_if.key)
            if parent is None:
                problems.append(f"{where}: it can only depend on a requirement listed before it.")
            elif parent.field_type not in ("yes_no", "select"):  # type: ignore[attr-defined]
                problems.append(f"{where}: it can only depend on a yes/no or choice requirement.")
        keys[req.key] = req
    return problems
