"""THE credit score (lib/calculations/credit-score.ts), used everywhere: farmer app, loans, admin.

Five factors from 0 to 100 are weighted by the admin's credit rules, then mapped onto 300-850.
The default rules reproduce the original fixed weights exactly (30/25/25/10/10).

Records and MoMo entries are typed in by the farmer, so only entries recorded on time build the
history, completeness and MoMo factors: an entry counts when it was entered within
RECORD_GRACE_DAYS of the date it describes. Months of "history" can't be made in one sitting by
back-dating, and a pile of entries typed in at once only counts for the recent ones. Older
entries are still kept and still count for income and the loan's record minimum.

Entries are also weighed by what AgriCtec checked: one verified from its receipt counts in full,
one nobody has checked yet counts for the rules' `unverified_weight` (50% by default), and one
that was rejected or is disputed counts for nothing. Typing in sales that never happened can't
buy a good score. Costs count in full unless rejected: nobody inflates their own costs."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.domain.clock import add_months, month_start, today
from app.domain.defaults import DEFAULT_CREDIT_CONFIG
from app.domain.fmt import js_round, round_int
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


# Statuses that take an entry out of the score: AgriCtec rejected it, or it is in dispute.
NOT_COUNTED = ("rejected", "disputed")


def entry_weight(item: FarmRecord | MomoTransaction, unverified_weight: int) -> float:
    """How much one entry counts: 1 when verified, 0 when rejected or disputed, and
    `unverified_weight`% while nobody has checked it (self-reported or waiting for review)."""
    status = item.verification_status
    if status == "verified":
        return 1.0
    if status in NOT_COUNTED:
        return 0.0
    return max(0, min(100, unverified_weight)) / 100


def _active_months(
    items: list[FarmRecord | MomoTransaction], unverified_weight: int, months: int, now: date
) -> float:
    """The last `months` months (this one included) with an entry: a month counts in full when
    one of its entries is verified, and by the best of its weights otherwise."""
    best: dict[tuple[int, int], float] = {}
    for item in items:
        key = (item.date.year, item.date.month)
        best[key] = max(best.get(key, 0.0), entry_weight(item, unverified_weight))
    this_month = month_start(now)
    return sum(
        best.get(((m := add_months(this_month, -i)).year, m.month), 0.0) for i in range(months)
    )


def _income_stability(records: list[FarmRecord], unverified_weight: int) -> float:
    """Recorded income against recorded costs. Income counts by its entry's weight; costs
    count in full unless they were rejected or are disputed."""
    income = sum(
        (Decimal(str(r.amount)) * Decimal(str(entry_weight(r, unverified_weight)))
         for r in records if r.type == "income"),
        Decimal("0"),
    )
    if income <= 0:
        return 0
    expenses = sum(
        (Decimal(str(r.amount)) for r in records
         if r.type == "expense" and r.verification_status not in NOT_COUNTED),
        Decimal("0"),
    )
    if income >= expenses:
        # The same 1-decimal profit margin as finance.profit_margin.
        return min(100, 50 + js_round(float((income - expenses) / income * 100), 1))
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
    unverified = targets.unverified_weight
    checks = verification_checks(profile)
    timely_records = [r for r in records if recorded_on_time(r)]
    timely_momo = [t for t in momo if recorded_on_time(t)]
    counted_records = sum(entry_weight(r, unverified) for r in timely_records)
    counted_momo = sum(entry_weight(t, unverified) for t in timely_momo)
    factors = {
        "payment_history": _active_months([*timely_records, *timely_momo], unverified, window, now) / window * 100,
        "record_completeness": min(100, (counted_records + counted_momo) / targets.records_for_full * 100),
        "income_stability": _income_stability(records, unverified),
        "momo_activity": min(100, counted_momo / targets.momo_for_full * 100),
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
    counts = f"verified entries count in full, unchecked ones {t.unverified_weight}%"
    text = {
        "payment_history": (
            "Payment history",
            f"Months, out of the last {t.months_window}, with at least one entry logged on time ({counts})",
            f"Log at least one entry in each of the last {t.months_window} months, with its receipt",
        ),
        "record_completeness": (
            "Record completeness",
            f"Records and MoMo entries logged within {RECORD_GRACE_DAYS} days, against a target of "
            f"{t.records_for_full} ({counts})",
            f"Aim for {t.records_for_full}+ records and MoMo entries, each logged within {RECORD_GRACE_DAYS} days "
            "and verified from its receipt",
        ),
        "income_stability": (
            "Income stability",
            f"Your recorded income compared with your recorded costs (unchecked income counts {t.unverified_weight}%)",
            "Keep your recorded income above your costs, and attach receipts so it is verified",
        ),
        "momo_activity": (
            "MoMo activity",
            f"Mobile Money entries logged on time, against a target of {t.momo_for_full} ({counts})",
            f"Log {t.momo_for_full}+ Mobile Money transactions with their receipts",
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
    weight = config.targets.unverified_weight
    unchecked = sum(1 for item in [*records, *momo] if 0 < entry_weight(item, weight) < 1)
    if unchecked and weight < 100:
        gaps.append(
            f"{unchecked} {'entry is' if unchecked == 1 else 'entries are'} not verified yet, so "
            f"{'it counts' if unchecked == 1 else 'they count'} for {weight}% of the score until "
            "AgriCtec checks the receipt."
        )
    not_counted = sum(1 for item in [*records, *momo] if item.verification_status in NOT_COUNTED)
    if not_counted:
        gaps.append(
            f"{not_counted} {'entry was' if not_counted == 1 else 'entries were'} rejected or "
            f"{'is' if not_counted == 1 else 'are'} disputed, so {'it doesn' if not_counted == 1 else 'they don'}'t "
            "count towards the score."
        )
    labels = ["Your farm profile is incomplete.", "Your phone number is not verified.",
              "Your farm location is not verified.", "Your Ghana Card is not verified."]
    gaps.extend(label for label, ok in zip(labels, verification_checks(profile), strict=True) if not ok)
    return gaps
