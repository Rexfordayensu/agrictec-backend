"""THE credit score (lib/calculations/credit-score.ts), used everywhere: farmer app, loans, admin.

Five factors from 0 to 100 are weighted by the admin's credit rules, then mapped onto 300-850.
The default rules reproduce the original fixed weights exactly (30/25/25/10/10).

Records and MoMo entries are typed in by the farmer, so only entries recorded on time build the
history, completeness and MoMo factors: an entry counts when it was entered within
RECORD_GRACE_DAYS of the date it describes. Months of "history" can't be made in one sitting by
back-dating, and a pile of entries typed in at once only counts for the recent ones. Older
entries are still kept and still count for income and the loan's record minimum."""

from datetime import date, datetime, timedelta
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

# How long after the day it describes an entry may be typed in and still build the score.
RECORD_GRACE_DAYS = 45


def recorded_on_time(item: FarmRecord | MomoTransaction) -> bool:
    """Entered within RECORD_GRACE_DAYS of its own date (never counted when back-dated further)."""
    entered = item.created_at.date() if isinstance(item.created_at, datetime) else item.created_at
    return entered - item.date <= timedelta(days=RECORD_GRACE_DAYS)


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
    return max(0, 50 - float(expenses / income - 1) * 100)


def verification_checks(profile: Profile | None) -> list[bool]:
    """Profile complete, phone (by SMS code), a farm location and a Ghana Card, both verified by
    an admin. A map pin alone doesn't count: anyone can drop one."""
    return [
        is_profile_complete(profile),
        bool(profile and profile.verification.phone),
        bool(profile and profile.verification.location),
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
    timely_records = [r for r in records if recorded_on_time(r)]
    timely_momo = [t for t in momo if recorded_on_time(t)]
    factors = {
        "payment_history": _active_months(timely_records, timely_momo, window, now) / window * 100,
        "record_completeness": min(100, (len(timely_records) + len(timely_momo)) / targets.records_for_full * 100),
        "income_stability": _income_stability(records),
        "momo_activity": min(100, len(timely_momo) / targets.momo_for_full * 100),
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


# ---------- Explaining the score (milestone 4): the formula above is unchanged ----------

SCORE_NAME = "AgriCtec score"
SCORE_RANGE = 550
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def score_factors(credit: CreditScoreBreakdown, config: CreditConfigInput) -> list[dict]:
    """Each factor: its weight, the points it adds and could add, what it measures and how to
    raise it, in the farmer's words (lib/calculations/score-steps.ts says the same)."""
    t, w = config.targets, config.weights
    text = {
        "payment_history": (
            "Payment history",
            f"Months, out of the last {t.months_window}, with at least one entry logged on time",
            f"Log at least one entry in each of the last {t.months_window} months",
        ),
        "record_completeness": (
            "Record completeness",
            f"Records and MoMo entries logged within {RECORD_GRACE_DAYS} days, against a target of {t.records_for_full}",
            f"Aim for {t.records_for_full}+ records and MoMo entries, each logged within {RECORD_GRACE_DAYS} days",
        ),
        "income_stability": (
            "Income stability",
            "Your recorded income compared with your recorded costs",
            "Keep your recorded income above your costs",
        ),
        "momo_activity": (
            "MoMo activity",
            f"Mobile Money entries logged on time, against a target of {t.momo_for_full}",
            f"Log {t.momo_for_full}+ Mobile Money transactions",
        ),
        "verification": (
            "Verification",
            "Profile complete, phone verified, farm location verified and Ghana Card verified",
            "Complete your profile and the verifications in Settings",
        ),
    }
    out = []
    for key in FACTOR_KEYS:
        weight = getattr(w, key)
        value = getattr(credit, key)
        max_points = round_int(weight / 100 * SCORE_RANGE)
        label, measures, improve = text[key]
        out.append({
            "key": key, "label": label, "weight": weight, "value": value,
            "points": round_int(value / 100 * max_points), "max_points": max_points,
            "measures": measures, "improve": improve,
        })
    return out


def score_gaps(
    records: list[FarmRecord],
    momo: list[MomoTransaction],
    profile: Profile | None,
    config: CreditConfigInput,
    *,
    verified_entries: int,
    entries_with_evidence: int,
    now: date | None = None,
) -> list[str]:
    """What is missing, in plain words, from the farmer's real data only."""
    now = now or today()
    gaps: list[str] = []
    timely = [r for r in records if recorded_on_time(r)] + [t for t in momo if recorded_on_time(t)]
    active = {(item.date.year, item.date.month) for item in timely}
    this_month = month_start(now)
    window = max(1, config.targets.months_window)
    empty = [add_months(this_month, -i) for i in range(window - 1, -1, -1)]
    empty = [m for m in empty if (m.year, m.month) not in active]
    if empty:
        names = [f"{MONTH_NAMES[m.month - 1]} {m.year}" for m in empty]
        listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
        gaps.append(f"No entries logged on time in {listed}.")
    late = len(records) + len(momo) - len(timely)
    if late:
        gaps.append(
            f"{late} {'entry was' if late == 1 else 'entries were'} logged more than {RECORD_GRACE_DAYS} days late, "
            "so they don't build the score."
        )
    if not momo:
        gaps.append("No Mobile Money entries yet.")
    entries = len(records) + len(momo)
    if entries and not entries_with_evidence:
        gaps.append("None of your entries has a receipt or statement attached.")
    if entries and not verified_entries:
        gaps.append("None of your entries is verified yet: attach evidence and request a review.")
    labels = ["Your farm profile is incomplete.", "Your phone number is not verified.",
              "Your farm location is not verified.", "Your Ghana Card is not verified."]
    gaps.extend(label for label, ok in zip(labels, verification_checks(profile), strict=True) if not ok)
    return gaps
