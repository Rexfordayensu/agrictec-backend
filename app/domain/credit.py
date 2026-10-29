"""THE credit score (lib/calculations/credit-score.ts), used everywhere: farmer app, loans, admin.

Five factors from 0 to 100 are weighted by the admin's credit rules, then mapped onto 300-850.
The default rules reproduce the original fixed weights exactly (30/25/25/10/10)."""

from datetime import date
from typing import Literal

from app.domain.clock import add_months, month_start, today
from app.domain.defaults import DEFAULT_CREDIT_CONFIG
from app.domain.finance import profit_margin, total_expenses, total_income
from app.domain.fmt import round_int
from app.schemas.farm import CreditScoreBreakdown, FarmRecord, MomoTransaction, Profile
from app.schemas.platform import CreditBands, CreditConfig, CreditConfigInput, CreditWeights

ScoreLabel = Literal["Building", "Fair", "Good", "Excellent"]
BANDS: list[ScoreLabel] = ["Building", "Fair", "Good", "Excellent"]
FACTOR_KEYS = [
    "payment_history",
    "record_completeness",
    "income_stability",
    "momo_activity",
    "verification",
]


def default_config() -> CreditConfig:
    return CreditConfig.model_validate(DEFAULT_CREDIT_CONFIG)


def is_profile_complete(profile: Profile | None) -> bool:
    """Name, phone, farm name and acres are all filled in."""
    return bool(
        profile
        and profile.name.strip()
        and profile.phone.strip()
        and profile.farm_name.strip()
        and profile.acres > 0
    )


def _active_months(
    records: list[FarmRecord], momo: list[MomoTransaction], months: int, now: date
) -> int:
    """How many of the last `months` months (this one included) have a record or MoMo entry."""
    active = {(item.date.year, item.date.month) for item in [*records, *momo]}
    this_month = month_start(now)
    return sum(
        1
        for i in range(months)
        if ((m := add_months(this_month, -i)).year, m.month) in active
    )


def _income_stability(records: list[FarmRecord]) -> float:
    income = total_income(records)
    if income <= 0:
        return 0
    expenses = total_expenses(records)
    if income >= expenses:
        return min(100, 50 + profit_margin(records))
    return max(0, 50 - (expenses / income - 1) * 100)


def verification_checks(profile: Profile | None) -> list[bool]:
    """Profile complete, phone, location (a map pin counts) and a Ghana Card verified by an admin."""
    return [
        is_profile_complete(profile),
        bool(profile and profile.verification.phone),
        bool(profile and (profile.gps or profile.verification.location)),
        bool(profile and profile.ghana_card.status == "verified"),
    ]


def calculate_credit_score(
    records: list[FarmRecord],
    momo: list[MomoTransaction],
    profile: Profile | None,
    config: CreditConfigInput | None = None,
    now: date | None = None,
) -> CreditScoreBreakdown:
    config = config or default_config()
    now = now or today()
    targets = config.targets
    window = max(1, targets.months_window)
    checks = verification_checks(profile)
    factors = {
        "payment_history": _active_months(records, momo, window, now) / window * 100,
        "record_completeness": min(100, (len(records) + len(momo)) / targets.records_for_full * 100),
        "income_stability": _income_stability(records),
        "momo_activity": min(100, len(momo) / targets.momo_for_full * 100),
        "verification": sum(checks) / len(checks) * 100,
    }
    return score_from_factors(factors, config.weights)


def score_from_factors(factors: dict[str, float], weights: CreditWeights) -> CreditScoreBreakdown:
    """Weights the five factors (0-100 each) and maps the result onto 300-850."""
    weighted = sum(factors[key] * getattr(weights, key) / 100 for key in FACTOR_KEYS)
    score = round_int(300 + min(100, max(0, weighted)) / 100 * 550)
    return CreditScoreBreakdown(
        score=score, **{key: round_int(factors[key]) for key in FACTOR_KEYS}
    )


def score_label(score: float, bands: CreditBands | None = None) -> ScoreLabel:
    """The band a score falls in, using the admin's thresholds (default 500 / 650 / 750)."""
    bands = bands or default_config().bands
    if score < bands.fair:
        return "Building"
    if score < bands.good:
        return "Fair"
    if score < bands.excellent:
        return "Good"
    return "Excellent"
