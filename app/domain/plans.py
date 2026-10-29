"""Subscription tiers (lib/subscriptions/plans.ts)."""

from datetime import UTC, datetime

from app.domain.clock import add_months
from app.domain.defaults import ALL_TOOLS, PLAN_CATALOG, fresh
from app.schemas.platform import Plan, PlanAccess

PLAN_IDS = ["free", "basic", "premium", "advanced"]
FEATURE_NAMES = {
    "pdfReports": "PDF downloads",
    "aiAssistant": "AI Assistant",
    "cropDoctor": "AI Crop Doctor",
    "yieldPredictor": "Yield Predictor",
}
# AI credits each use costs.
AI_COSTS = {"aiAssistant": 1, "cropDoctor": 5}


def normalize_plans(saved: list[dict] | None) -> list[dict]:
    """Every tier, in catalogue order, with the saved names and prices kept."""
    out = []
    for base in fresh(PLAN_CATALOG):
        old = next((p for p in saved or [] if p.get("id") == base["id"]), None) or {}
        merged = {**base, **old, "id": base["id"]}
        if base["id"] == "free":
            merged["priceGHS"] = 0
            merged["active"] = True
        merged["access"] = {**base["access"], **(old.get("access") or {})}
        merged["aiCredits"] = old["aiCredits"] if "aiCredits" in old else base["aiCredits"]
        out.append(merged)
    return out


def plan_access(plan: Plan) -> PlanAccess:
    """What a plan unlocks. Free and Basic never include the AI tools; Advanced has everything."""
    if plan.id == "free":
        return PlanAccess(pdf_reports=False, ai_assistant=False, crop_doctor=False, yield_predictor=False)
    if plan.id == "basic":
        pdf = plan.access.pdf_reports if plan.access else True
        return PlanAccess(pdf_reports=pdf, ai_assistant=False, crop_doctor=False, yield_predictor=False)
    if plan.id == "advanced":
        return PlanAccess.model_validate(ALL_TOOLS)
    base = next(p for p in PLAN_CATALOG if p["id"] == plan.id)["access"]
    return PlanAccess.model_validate({**base, **(plan.access.model_dump(by_alias=True) if plan.access else {})})


def credits_limit(plan: Plan) -> int | None:
    """Monthly AI credits: None is unlimited (Advanced)."""
    if plan.id == "advanced":
        return None
    if plan.id == "premium":
        return plan.ai_credits if plan.ai_credits is not None else 100
    return 0


def next_billing_date(now: datetime) -> datetime:
    """Same day next month, clamped to the month's final day. No automatic renewal."""
    day = add_months(now.astimezone(UTC).date(), 1)
    return now.astimezone(UTC).replace(year=day.year, month=day.month, day=day.day)


def month_key(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m")


def next_month_start(now: datetime) -> datetime:
    first = add_months(now.astimezone(UTC).date().replace(day=1), 1)
    return datetime(first.year, first.month, 1, tzinfo=UTC)
