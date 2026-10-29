"""Fraud and risk detection (lib/admin/risk-rules.ts). Pure: every farmer's data in, suspected
issues out. The cross-account rules compare a MoMo reference, a phone or a Ghana Card across
every account."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from statistics import median

from app.domain.clock import add_months, as_date
from app.domain.fmt import format_date, format_ghs, list_of, month_label, round_int
from app.domain.verification import normalize_phone
from app.schemas.farm import FarmRecord, MomoTransaction, Profile

THRESHOLDS = {
    # One amount more than this many times the farmer's median for that record type...
    "outlier_multiple": 5,
    # ...and above this amount (GHS); a median needs at least this many amounts.
    "outlier_min_amount": 5000,
    "outlier_min_samples": 5,
    # More than this many records created on the same day.
    "burst_per_day": 10,
    # A month's income more than this many times the previous 3 months' average (= 300% above)...
    "spike_multiple": 4,
    "spike_min_amount": 10000,
    "spike_lookback_months": 3,
    "spike_min_baseline_months": 2,
    # More than this many records dated over `backdated_days` before they were entered.
    "backdated_count": 15,
    "backdated_days": 90,
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}


@dataclass
class RiskSubject:
    user_id: str
    profile: Profile
    records: list[FarmRecord]
    momo: list[MomoTransaction]


@dataclass
class DetectedRisk:
    user_id: str
    rule: str
    severity: str
    description: str
    related_ids: list[str] = field(default_factory=list)


@dataclass
class RiskRule:
    id: str
    label: str
    explanation: str
    guidance: str
    severity: str
    # What the related IDs point to: 'momo', 'records' or 'accounts'.
    related: str
    detect: Callable[[list[RiskSubject]], list[DetectedRisk]]


def _group_by[T](items: list[T], key: Callable[[T], str]) -> dict[str, list[T]]:
    groups: dict[str, list[T]] = {}
    for item in items:
        k = key(item)
        if k:
            groups.setdefault(k, []).append(item)
    return groups


def _ids(items) -> list[str]:
    return [item.id for item in items]


def _name_of(s: RiskSubject) -> str:
    return s.profile.name.strip() or "an unnamed farmer"


def _created_day(r: FarmRecord) -> date:
    return as_date(r.created_at)


def _shared_value(
    subjects: list[RiskSubject],
    rule: str,
    severity: str,
    value: Callable[[RiskSubject], str],
    describe: Callable[[RiskSubject, list[RiskSubject]], str],
) -> list[DetectedRisk]:
    """One flag per account in each group of two or more accounts sharing a value."""
    flags = []
    for group in _group_by(subjects, value).values():
        if len(group) < 2:
            continue
        for subject in group:
            others = [s for s in group if s.user_id != subject.user_id]
            flags.append(
                DetectedRisk(
                    subject.user_id, rule, severity, describe(subject, others), [s.user_id for s in others]
                )
            )
    return flags


def _also_on(others: list[RiskSubject]) -> str:
    lead = "the account of" if len(others) == 1 else f"{len(others)} other accounts:"
    return f"{lead} {list_of([_name_of(o) for o in others])}"


def _duplicate_momo(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    owner: dict[str, str] = {}
    every = []
    for s in subjects:
        for t in s.momo:
            owner[t.id] = s.user_id
            every.append(t)
    flags = []
    for reference, group in _group_by(every, lambda t: t.reference.strip().upper()).items():
        if len(group) < 2:
            continue
        accounts = list(dict.fromkeys(owner[t.id] for t in group))
        amounts = list_of([format_ghs(t.amount) for t in group])
        across = f" across {len(accounts)} accounts" if len(accounts) > 1 else ""
        for user_id in accounts:
            flags.append(
                DetectedRisk(
                    user_id,
                    "duplicate_momo_ref",
                    "high",
                    f"Reference {reference} appears on {len(group)} transactions{across} ({amounts}).",
                    _ids(group),
                )
            )
    return flags


def _amount_outlier(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    t = THRESHOLDS
    flags = []
    for s in subjects:
        for kind in ("income", "expense"):
            of_type = [r for r in s.records if r.type == kind and r.amount > 0]
            if len(of_type) < t["outlier_min_samples"]:
                continue
            typical = median(r.amount for r in of_type)
            for r in of_type:
                if r.amount > t["outlier_min_amount"] and r.amount > typical * t["outlier_multiple"]:
                    label = "Income" if kind == "income" else "Expense"
                    flags.append(
                        DetectedRisk(
                            s.user_id,
                            "amount_outlier",
                            "medium",
                            f"{label} of {format_ghs(r.amount)} on {format_date(r.date)} is "
                            f"{round_int(r.amount / typical)}× this farmer’s median {kind} ({format_ghs(typical)}).",
                            [r.id],
                        )
                    )
    return flags


def _burst(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    flags = []
    for s in subjects:
        for day, group in _group_by(s.records, lambda r: _created_day(r).isoformat()).items():
            if len(group) > THRESHOLDS["burst_per_day"]:
                flags.append(
                    DetectedRisk(
                        s.user_id,
                        "burst_entries",
                        "medium",
                        f"{len(group)} records were entered on {format_date(date.fromisoformat(day))}.",
                        _ids(group),
                    )
                )
    return flags


def _income_spike(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    t = THRESHOLDS
    flags = []
    for s in subjects:
        months = _group_by(
            [r for r in s.records if r.type == "income" and r.amount > 0],
            lambda r: r.date.isoformat()[:7],
        )

        def total(month: str) -> float:
            return sum(r.amount for r in months.get(month, []))

        for month, group in months.items():
            amount = total(month)
            if amount <= t["spike_min_amount"]:
                continue
            start = date.fromisoformat(f"{month}-01")
            baseline = [
                value
                for i in range(t["spike_lookback_months"])
                if (value := total(add_months(start, -(i + 1)).isoformat()[:7])) > 0
            ]
            if len(baseline) < t["spike_min_baseline_months"]:
                continue
            average = sum(baseline) / len(baseline)
            if amount <= average * t["spike_multiple"]:
                continue
            flags.append(
                DetectedRisk(
                    s.user_id,
                    "income_spike",
                    "medium",
                    f"Income in {month_label(start, long=True)} was {format_ghs(amount)}, "
                    f"{round_int((amount - average) / average * 100)}% above the previous "
                    f"{t['spike_lookback_months']} months’ average ({format_ghs(average)}).",
                    _ids(group),
                )
            )
    return flags


def _shared_phone(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    return _shared_value(
        subjects,
        "shared_phone",
        "high",
        lambda s: normalize_phone(s.profile.phone) if s.profile.phone.strip() else "",
        lambda s, others: f"Phone {normalize_phone(s.profile.phone)} is also on {_also_on(others)}.",
    )


def _shared_card(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    def describe(s: RiskSubject, others: list[RiskSubject]) -> str:
        masked = s.profile.ghana_card.masked
        return f"The same Ghana Card{f' ({masked})' if masked else ''} is also on {_also_on(others)}."

    return _shared_value(
        subjects,
        "shared_ghana_card",
        "high",
        lambda s: (s.profile.ghana_card.hash or "")
        if s.profile.ghana_card.status != "not_provided"
        else "",
        describe,
    )


def _backdated(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    t = THRESHOLDS
    flags = []
    for s in subjects:
        late = [r for r in s.records if (_created_day(r) - r.date).days > t["backdated_days"]]
        if len(late) <= t["backdated_count"]:
            continue
        worst = max((_created_day(r) - r.date).days for r in late)
        flags.append(
            DetectedRisk(
                s.user_id,
                "backdated_bulk",
                "low",
                f"{len(late)} records were entered more than {t['backdated_days']} days after their "
                f"date (up to {worst} days late).",
                _ids(late),
            )
        )
    return flags


RISK_RULES: list[RiskRule] = [
    RiskRule(
        "duplicate_momo_ref",
        "Duplicate MoMo reference",
        "The same MoMo reference is used on two or more transactions, in one account or across accounts.",
        "Every Mobile Money transfer has its own reference, so one reference on two transactions usually means the same payment was entered twice, perhaps to make income look bigger. Compare the amounts, dates and counterparties; a genuine typo is common, so ask the farmer before confirming.",
        "high",
        "momo",
        _duplicate_momo,
    ),
    RiskRule(
        "amount_outlier",
        "Unusual amount",
        f"One amount more than {THRESHOLDS['outlier_multiple']}× the farmer’s median for that type, and above {format_ghs(THRESHOLDS['outlier_min_amount'])}.",
        "A single sale or expense far bigger than everything else the farmer records. It can be real (a tractor, a whole harvest sold at once) or a typing mistake with an extra zero, or an invented sale. Look at the description and ask for a receipt if it matters for a loan.",
        "medium",
        "records",
        _amount_outlier,
    ),
    RiskRule(
        "burst_entries",
        "Burst of entries",
        f"More than {THRESHOLDS['burst_per_day']} farm records created on the same day.",
        "Many records typed in at once. Often harmless (a farmer catching up after a busy week, or a cooperative entering for its members), but it is also how a history gets made up just before a loan application. Check whether the dates and amounts look natural.",
        "medium",
        "records",
        _burst,
    ),
    RiskRule(
        "income_spike",
        "Income spike",
        f"A month’s income more than 300% above the previous {THRESHOLDS['spike_lookback_months']} months’ average, and above {format_ghs(THRESHOLDS['spike_min_amount'])}.",
        "Income suddenly jumps far above what the farmer usually earns. Harvest months do bring big sales, so compare with the crop and season; a spike just before a loan application, with no matching MoMo receipts, deserves a closer look.",
        "medium",
        "records",
        _income_spike,
    ),
    RiskRule(
        "shared_phone",
        "Shared phone number",
        "The same phone number is on more than one account.",
        "A phone (and its MoMo wallet) normally belongs to one farmer. Two accounts on one number can be a family sharing a phone, or one person running several accounts to borrow more than once. Compare the two accounts side by side.",
        "high",
        "accounts",
        _shared_phone,
    ),
    RiskRule(
        "shared_ghana_card",
        "Shared Ghana Card",
        "The same Ghana Card (matched by its fingerprint) is on more than one account.",
        "One Ghana Card belongs to one person. Matching is done on a one-way fingerprint of the full number, never the number itself. Two accounts with the same card is a strong sign of a duplicate or borrowed identity: compare the accounts and check which one the card really belongs to.",
        "high",
        "accounts",
        _shared_card,
    ),
    RiskRule(
        "backdated_bulk",
        "Backdated bulk entry",
        f"More than {THRESHOLDS['backdated_count']} records dated over {THRESHOLDS['backdated_days']} days before they were entered.",
        "Old activity entered long after it happened. New farmers often add their past season when they join, which is fine; but a large old history appearing all at once can be invented to pass the record requirement for a loan. Check the dates against the farmer’s sign-up and season.",
        "low",
        "records",
        _backdated,
    ),
]

RULES_BY_ID = {rule.id: rule for rule in RISK_RULES}
RISK_RULE_LABELS = {rule.id: rule.label for rule in RISK_RULES}


def detect_risks(subjects: list[RiskSubject]) -> list[DetectedRisk]:
    return [risk for rule in RISK_RULES for risk in rule.detect(subjects)]


def risk_key(rule: str, user_id: str, related_ids: list[str]) -> str:
    """Identifies "the same finding": a scan never re-adds a flag that already exists."""
    return f"{rule}|{user_id}|{','.join(sorted(related_ids))}"
