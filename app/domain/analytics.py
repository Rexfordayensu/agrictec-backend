"""Platform-wide numbers for the admin overview (lib/admin/analytics.ts, lib/admin/overview.ts).

Pure functions over farmer accounts, applications, flags and verifications. Guest accounts are
left out: they are not real farmers."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from app.domain.clock import add_months, as_date, month_start
from app.domain.constants import REGIONS
from app.domain.fmt import js_round, month_label, plural, round_int
from app.domain.loans import APPROVED_LOAN_STATUSES, LOAN_STATUS_LABELS, LOAN_STATUSES
from app.domain.permissions import can
from app.schemas.admin import FarmerAccount, RiskFlag
from app.schemas.farm import LoanApplication, VerificationRequest
from app.schemas.platform import CreditBands, FeatureFlags

DateRange = Literal["30d", "90d", "12m", "all"]
LOAN_WAIT_HOURS = 48
FUNNEL_STAGES = ["submitted", "under_review", "approved", "disbursed", "repaid"]


def registered(farmers: list[FarmerAccount]) -> list[FarmerAccount]:
    return [f for f in farmers if not f.is_guest]


def percent_change(current: float, previous: float) -> float | None:
    """% change to 1 decimal; None when there is no baseline."""
    if previous == 0:
        return 0 if current == 0 else None
    return js_round((current - previous) / abs(previous) * 100, 1)


def _approvedish(a: LoanApplication) -> bool:
    return a.status in APPROVED_LOAN_STATUSES


def range_window(r: DateRange, now: datetime) -> tuple[datetime | None, datetime | None, datetime]:
    """(start, previous_start, end): the range and the equally long period before it."""
    if r == "all":
        return None, None, now
    if r == "12m":
        start_day = add_months(now.date(), -12)
        start = now.replace(year=start_day.year, month=start_day.month, day=start_day.day)
    else:
        start = now - timedelta(days=30 if r == "30d" else 90)
    return start, start - (now - start), now


def _within(value: datetime, start: datetime | None, end: datetime) -> bool:
    return (start is None or value >= start) and value < end


def _days_since(value: datetime, now: datetime) -> int:
    return (now.date() - as_date(value)).days


def score_buckets(bands: CreditBands) -> list[dict]:
    edges = [300, bands.fair, bands.good, bands.excellent, 851]
    names = ["Building", "Fair", "Good", "Excellent"]
    return [
        {"band": name, "label": f"{edges[i]}–{edges[i + 1] - 1}", "min": edges[i], "max": edges[i + 1] - 1}
        for i, name in enumerate(names)
    ]


def score_distribution(farmers: list[FarmerAccount], bands: CreditBands) -> list[dict]:
    people = registered(farmers)
    return [
        {**b, "count": sum(1 for f in people if b["min"] <= f.stats.score <= b["max"])}
        for b in score_buckets(bands)
    ]


def farmers_by_region(farmers: list[FarmerAccount]) -> list[dict]:
    counts = dict.fromkeys(REGIONS, 0)
    for f in registered(farmers):
        counts[f.profile.region] = counts.get(f.profile.region, 0) + 1
    return sorted(
        ({"region": r, "count": c} for r, c in counts.items()), key=lambda x: (-x["count"], x["region"])
    )


def crop_popularity(farmers: list[FarmerAccount]) -> list[dict]:
    counts: dict[str, int] = {}
    for f in registered(farmers):
        for crop in set(f.profile.primary_crops):
            counts[crop] = counts.get(crop, 0) + 1
    return sorted(({"crop": c, "count": n} for c, n in counts.items()), key=lambda x: (-x["count"], x["crop"]))


def loan_funnel(apps: list[LoanApplication]) -> list[dict]:
    return [
        {"status": s, "label": LOAN_STATUS_LABELS[s], "count": sum(1 for a in apps if a.status == s)}
        for s in LOAN_STATUSES
    ]


def kpis(
    farmers: list[FarmerAccount],
    apps: list[LoanApplication],
    flags: list[RiskFlag],
    verifications: list[VerificationRequest],
    now: datetime,
) -> dict:
    """The headline figures of the platform report."""
    people = registered(farmers)
    this_month = month_start(now.date())
    last_month = add_months(this_month, -1)
    new_this = sum(1 for f in people if month_start(as_date(f.created_at)) == this_month)
    new_last = sum(1 for f in people if month_start(as_date(f.created_at)) == last_month)
    approved = [a for a in apps if _approvedish(a)]
    decided = len(approved) + sum(1 for a in apps if a.status == "rejected")
    return {
        "totalFarmers": len(people),
        "activeLast30Days": sum(
            1 for f in people if f.last_active_at and _days_since(f.last_active_at, now) <= 30
        ),
        "newThisMonth": new_this,
        "newLastMonth": new_last,
        "newChangePct": percent_change(new_this, new_last),
        "totalIncome": sum(f.stats.income for f in people),
        "averageScore": round_int(sum(f.stats.score for f in people) / len(people)) if people else 0,
        "loanRequested": sum(a.amount for a in apps),
        "loanApproved": sum(a.amount for a in approved),
        "approvalRate": round_int(len(approved) / decided * 100) if decided else None,
        "pendingVerifications": sum(1 for v in verifications if v.status == "pending"),
        "openHighRisk": sum(1 for f in flags if f.status == "open" and f.severity == "high"),
    }


def overview_kpis(
    farmers: list[FarmerAccount],
    apps: list[LoanApplication],
    flags: list[RiskFlag],
    verifications: list[VerificationRequest],
    r: DateRange,
    now: datetime,
) -> dict:
    people = registered(farmers)
    start, previous_start, end = range_window(r, now)

    def joined(a: datetime | None, b: datetime) -> int:
        return sum(1 for f in people if _within(f.created_at, a, b))

    def apps_in(a: datetime | None, b: datetime) -> list[LoanApplication]:
        return [x for x in apps if _within(x.created_at, a, b)]

    range_apps = apps_in(start, end)
    decided = [a for a in range_apps if _approvedish(a) or a.status == "rejected"]
    approved = [a for a in range_apps if _approvedish(a)]
    active30 = sum(1 for f in people if f.last_active_at and _days_since(f.last_active_at, now) <= 30)
    pending = [v for v in verifications if v.status == "pending"]
    open_flags = [f for f in flags if f.status == "open"]
    months = [
        datetime.combine(add_months(month_start(now.date()), -(11 - i)), datetime.min.time(), UTC)
        for i in range(12)
    ]

    def next_month(m: datetime) -> datetime:
        return datetime.combine(add_months(m.date(), 1), datetime.min.time(), UTC)

    requested = sum(a.amount for a in range_apps)
    return {
        "totalFarmers": len(people),
        "newInRange": joined(start, end),
        "newDelta": percent_change(joined(start, end), joined(previous_start, start))
        if start and previous_start
        else None,
        "active30": active30,
        "activeShare": round_int(active30 / len(people) * 100) if people else 0,
        "averageScore": round_int(sum(f.stats.score for f in people) / len(people)) if people else 0,
        "incomeTracked": sum(f.stats.income for f in people),
        "earningFarmers": sum(1 for f in people if f.stats.income > 0),
        "loanRequested": requested,
        "loanCount": len(range_apps),
        "loanDelta": percent_change(requested, sum(a.amount for a in apps_in(previous_start, start)))
        if start and previous_start
        else None,
        "approvalRate": round_int(len(approved) / len(decided) * 100) if decided else None,
        "approvedCount": len(approved),
        "decidedCount": len(decided),
        "pendingVerifications": len(pending),
        "oldestPendingDays": max((_days_since(v.submitted_at, now) for v in pending), default=None),
        "openFlags": len(open_flags),
        "openHighFlags": sum(1 for f in open_flags if f.severity == "high"),
        "farmersTrend": [sum(1 for f in people if f.created_at < next_month(m)) for m in months],
        "loanTrend": [sum(a.amount for a in apps_in(m, next_month(m))) for m in months],
    }


def signup_series(farmers: list[FarmerAccount], r: DateRange, now: datetime) -> dict:
    """Daily for 30 days, weekly for 90 days, monthly for 12 months and all time."""
    people = registered(farmers)
    midnight = datetime.combine(now.date(), datetime.min.time(), UTC)
    if r == "30d":
        unit = "day"
        first = midnight - timedelta(days=29)
        buckets = [(first + timedelta(days=i), first + timedelta(days=i + 1)) for i in range(30)]
        label = lambda d: f"{d.day} {month_label(d.date()).split()[0]}"  # noqa: E731
    elif r == "90d":
        unit = "week"
        start = midnight - timedelta(days=90)
        first = start - timedelta(days=start.weekday())  # weeks start on Monday
        count = -(-int((now - first).total_seconds()) // (7 * 86400))
        buckets = [(first + timedelta(weeks=i), first + timedelta(weeks=i + 1)) for i in range(count)]
        label = lambda d: f"{d.day} {month_label(d.date()).split()[0]}"  # noqa: E731
    else:
        unit = "month"
        earliest = min((as_date(f.created_at) for f in people), default=None)
        first_month = month_start(earliest) if r == "all" and earliest else add_months(month_start(now.date()), -11)
        months = max(2, (now.year - first_month.year) * 12 + now.month - first_month.month + 1)
        starts = [add_months(first_month, i) for i in range(months)]
        buckets = [
            (datetime.combine(s, datetime.min.time(), UTC), datetime.combine(add_months(s, 1), datetime.min.time(), UTC))
            for s in starts
        ]
        label = lambda d: month_label(d.date(), year="yy")  # noqa: E731
    return {
        "labels": [label(b[0]) for b in buckets],
        "counts": [sum(1 for f in people if _within(f.created_at, b[0], b[1])) for b in buckets],
        "unit": unit,
    }


def loan_stage_reach(apps: list[LoanApplication], r: DateRange, now: datetime) -> dict:
    """How far applications made in the range got (from their history), plus the losses."""
    start, _, end = range_window(r, now)
    in_range = [a for a in apps if _within(a.created_at, start, end)]

    def reached(status: str) -> int:
        return sum(1 for a in in_range if a.status == status or any(h.status == status for h in a.history))

    stages = [{"status": s, "label": LOAN_STATUS_LABELS[s], "reached": reached(s)} for s in FUNNEL_STAGES]
    for i, stage in enumerate(stages):
        previous = stages[i - 1]["reached"] if i else 0
        stage["conversion"] = round_int(stage["reached"] / previous * 100) if i and previous else None
    return {
        "stages": stages,
        "rejected": sum(1 for a in in_range if a.status == "rejected"),
        "defaulted": sum(1 for a in in_range if a.status == "defaulted"),
        "total": len(in_range),
    }


def has_appealed(f: FarmerAccount) -> bool:
    """A suspended farmer tagged "appeal", or with an admin note mentioning an appeal."""
    return f.status == "suspended" and (
        "appeal" in f.tags or any("appeal" in n.text.lower() for n in f.admin_notes)
    )


def needs_attention(
    *,
    farmers: list[FarmerAccount],
    apps: list[LoanApplication],
    flags: list[RiskFlag],
    verifications: list[VerificationRequest],
    feature_flags: FeatureFlags | None,
    role: str,
    now: datetime,
) -> list[dict]:
    """The prioritized to-do list, with only what this role can act on."""
    items: list[dict] = []
    if feature_flags and feature_flags.maintenance_mode:
        items.append(
            {
                "key": "maintenance",
                "count": 1,
                "text": "Maintenance mode is ON. Farmers can’t use the app.",
                "href": "/admin/settings",
                "urgent": True,
                "actionable": can(role, "settings.manage"),
            }
        )
    high = sum(1 for f in flags if f.status == "open" and f.severity == "high")
    if high and can(role, "risk.review"):
        items.append(
            {
                "key": "risk",
                "count": high,
                "text": f"{high} high-risk {plural(high, 'flag needs', 'flags need')} review",
                "href": "/admin/risk?severity=high&status=open",
                "actionable": True,
            }
        )
    pending = [v for v in verifications if v.status == "pending"]
    if pending and can(role, "verifications.review"):
        oldest = max(_days_since(v.submitted_at, now) for v in pending)
        items.append(
            {
                "key": "verifications",
                "count": len(pending),
                "text": f"{len(pending)} {plural(len(pending), 'verification', 'verifications')} pending (oldest {oldest} {plural(oldest, 'day', 'days')})",
                "href": "/admin/verifications",
                "actionable": True,
            }
        )
    waiting = sum(
        1
        for a in apps
        if a.status in ("submitted", "under_review")
        and (now - (a.history[-1].at if a.history else a.created_at)).total_seconds() >= LOAN_WAIT_HOURS * 3600
    )
    if waiting and can(role, "loans.manage"):
        items.append(
            {
                "key": "loans",
                "count": waiting,
                "text": f"{waiting} loan {plural(waiting, 'application', 'applications')} waiting more than {LOAN_WAIT_HOURS}h",
                "href": "/admin/loans?status=submitted",
                "actionable": True,
            }
        )
    appeals = sum(1 for f in farmers if has_appealed(f))
    if appeals and can(role, "farmers.suspend"):
        items.append(
            {
                "key": "appeals",
                "count": appeals,
                "text": f"{appeals} suspended {plural(appeals, 'farmer', 'farmers')} appealed",
                "href": "/admin/farmers?status=suspended&tag=appeal",
                "actionable": True,
            }
        )
    return items

