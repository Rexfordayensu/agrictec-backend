"""Ready-made admin reports (lib/admin/admin-reports.ts) and the custom report builder
(lib/admin/report-builder.ts). No column ever exposes a password hash or a Ghana Card number."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.analytics import kpis, registered, score_distribution
from app.domain.clock import add_months, as_date, iso, month_start
from app.domain.constants import REGIONS
from app.domain.csv import rows_to_csv, to_csv
from app.domain.fmt import format_ghs, month_label, round_int
from app.domain.loans import APPROVED_LOAN_STATUSES, LOAN_STATUS_LABELS, LOAN_STATUSES, loan_stats
from app.domain.risk_rules import RISK_RULES, SEVERITY_LABELS
from app.schemas.admin import FarmerAccount, RiskFlag
from app.schemas.farm import FarmRecord, LoanApplication, MomoTransaction, VerificationRequest
from app.schemas.platform import CreditBands, Lender

REPORT_CATALOG = [
    {"id": "platform", "title": "Platform Summary", "description": "Headline KPIs, sign-ups over the last 12 months and farmers per region."},
    {"id": "regions", "title": "Farmers by Region", "description": "Farmers, average credit score, income and Ghana Card verification per region."},
    {"id": "loans", "title": "Loan Portfolio", "description": "Applications by status, lender and region, with approval and default rates."},
    {"id": "credit", "title": "Credit Score Report", "description": "The score distribution by band, and the average score per region and per crop."},
    {"id": "verifications", "title": "Verification Report", "description": "Volumes by check, approval rate and average review time."},
    {"id": "risk", "title": "Risk Report", "description": "Risk flags by rule and outcome (open, dismissed, confirmed), with severities."},
]
GHANA_CARD_LABELS = {
    "not_provided": "Not provided",
    "pending": "Pending review",
    "verified": "Verified",
    "rejected": "Rejected",
}
KIND_LABELS = {"ghanaCard": "Ghana Card", "phone": "Phone", "location": "Location"}


@dataclass
class ReportData:
    farmers: list[FarmerAccount]
    records: list[FarmRecord]
    momo: list[MomoTransaction]
    applications: list[LoanApplication]
    lenders: list[Lender]
    verifications: list[VerificationRequest]
    risk_flags: list[RiskFlag]
    bands: CreditBands


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0


def _pct(part: float, whole: float) -> int:
    return round_int(part / whole * 100) if whole else 0


def _pct_label(value: float | None) -> str:
    return "—" if value is None else f"{round_int(value * 100)}%"


def _money(value: float) -> str:
    return format_ghs(round_int(value))


def _table(title: str, headers: list[str], rows: list[list[Any]], numeric: list[int]) -> dict:
    return {"title": title, "headers": headers, "rows": rows, "numeric": numeric}


def _approval_rate(apps: list[LoanApplication]) -> float | None:
    approved = sum(1 for a in apps if a.status in APPROVED_LOAN_STATUSES)
    decided = approved + sum(1 for a in apps if a.status == "rejected")
    return approved / decided if decided else None


def crop_names(profile) -> list[str]:
    """The farmer's crops for display: "Other" replaced by what they typed."""
    names = []
    for crop in profile.primary_crops:
        if crop != "Other":
            names.append(crop)
        elif (profile.other_crops or "").strip():
            names.append(profile.other_crops.strip())
    return names


def _platform(d: ReportData, now: datetime) -> dict:
    people = registered(d.farmers)
    k = kpis(d.farmers, d.applications, d.risk_flags, d.verifications, now)
    months = [add_months(month_start(now.date()), -(11 - i)) for i in range(12)]
    region_rows = sorted(
        ([r, n, f"{_pct(n, len(people))}%"] for r in REGIONS for n in [sum(1 for f in people if f.profile.region == r)]),
        key=lambda row: -row[1],
    )
    return {
        "summary": [
            ("Farmers", str(k["totalFarmers"])),
            ("Active (30 days)", str(k["activeLast30Days"])),
            ("New this month", str(k["newThisMonth"])),
            ("Average score", str(k["averageScore"])),
            ("Recorded income", _money(k["totalIncome"])),
            ("Loans requested", _money(k["loanRequested"])),
            ("Approval rate", "—" if k["approvalRate"] is None else f"{k['approvalRate']}%"),
            ("Pending verifications", str(k["pendingVerifications"])),
        ],
        "tables": [
            _table(
                "Sign-ups by month",
                ["Month", "New farmers"],
                [[month_label(m), sum(1 for f in people if month_start(as_date(f.created_at)) == m)] for m in months],
                [1],
            ),
            _table("Farmers by region", ["Region", "Farmers", "Share"], region_rows, [1, 2]),
        ],
    }


def _regions(d: ReportData, now: datetime) -> dict:
    people = registered(d.farmers)
    rows = []
    for region in REGIONS:
        here = [f for f in people if f.profile.region == region]
        verified = sum(1 for f in here if f.profile.ghana_card.status == "verified")
        rows.append(
            {
                "region": region,
                "count": len(here),
                "score": round_int(_avg([f.stats.score for f in here])),
                "income": sum(f.stats.income for f in here),
                "verified": _pct(verified, len(here)),
            }
        )
    rows.sort(key=lambda r: -r["count"])
    verified_all = sum(1 for f in people if f.profile.ghana_card.status == "verified")
    return {
        "summary": [
            ("Farmers", str(len(people))),
            ("Regions covered", f"{sum(1 for r in rows if r['count'])} of 16"),
            ("Ghana Card verified", f"{_pct(verified_all, len(people))}%"),
            ("Recorded income", _money(sum(r["income"] for r in rows))),
        ],
        "tables": [
            _table(
                "Per region",
                ["Region", "Farmers", "Average score", "Income (GHS)", "Ghana Card verified"],
                [
                    [r["region"], r["count"], r["score"] if r["count"] else "—", round_int(r["income"]), f"{r['verified']}%" if r["count"] else "—"]
                    for r in rows
                ],
                [1, 2, 3, 4],
            )
        ],
    }


def _loans(d: ReportData, now: datetime) -> dict:
    apps = d.applications
    stats = loan_stats(apps)
    region_of = {f.user_id: f.profile.region for f in d.farmers}
    lender_name = {lender.id: lender.name for lender in d.lenders}

    def grouped(key: Callable[[LoanApplication], str]) -> list[list[Any]]:
        groups: dict[str, list[LoanApplication]] = {}
        for a in apps:
            groups.setdefault(key(a), []).append(a)
        rows = [
            [name, len(items), round_int(sum(a.amount for a in items)), _pct_label(_approval_rate(items))]
            for name, items in groups.items()
        ]
        return sorted(rows, key=lambda row: -row[1])

    return {
        "summary": [
            ("Applications", str(len(apps))),
            ("Requested", _money(stats["totalRequested"])),
            ("Approved", _money(stats["approvedAmount"])),
            ("Disbursed", _money(stats["disbursedAmount"])),
            ("Approval rate", _pct_label(_approval_rate(apps))),
            ("Default rate", _pct_label(stats["defaultRate"])),
        ],
        "tables": [
            _table(
                "By status",
                ["Status", "Applications", "Amount (GHS)"],
                [
                    [LOAN_STATUS_LABELS[s], len(items), round_int(sum(a.amount for a in items))]
                    for s in LOAN_STATUSES
                    for items in [[a for a in apps if a.status == s]]
                ],
                [1, 2],
            ),
            _table(
                "By lender",
                ["Lender", "Applications", "Amount (GHS)", "Approval rate"],
                grouped(lambda a: lender_name.get(a.partner_id, "Unknown lender")),
                [1, 2, 3],
            ),
            _table(
                "By region",
                ["Region", "Applications", "Amount (GHS)", "Approval rate"],
                grouped(lambda a: region_of.get(a.user_id) or "Unknown"),
                [1, 2, 3],
            ),
        ],
    }


def _credit(d: ReportData, now: datetime) -> dict:
    people = registered(d.farmers)
    distribution = score_distribution(d.farmers, d.bands)
    scores = sorted(f.stats.score for f in people)
    by_crop: dict[str, list[int]] = {}
    for f in people:
        for crop in crop_names(f.profile):
            by_crop.setdefault(crop, []).append(f.stats.score)
    good_or_better = sum(b["count"] for b in distribution if b["band"] in ("Good", "Excellent"))
    region_rows = []
    for region in REGIONS:
        here = [f.stats.score for f in people if f.profile.region == region]
        region_rows.append([region, len(here), round_int(_avg(here)) if here else "—"])
    return {
        "summary": [
            ("Farmers scored", str(len(people))),
            ("Average score", str(round_int(_avg(scores)))),
            ("Median score", str(scores[len(scores) // 2] if scores else 0)),
            ("Good or better", f"{_pct(good_or_better, len(people))}%"),
        ],
        "tables": [
            _table(
                "Distribution",
                ["Band", "Score range", "Farmers", "Share"],
                [[b["band"], b["label"], b["count"], f"{_pct(b['count'], len(people))}%"] for b in distribution],
                [2, 3],
            ),
            _table("Average per region", ["Region", "Farmers", "Average score"], sorted(region_rows, key=lambda r: -r[1]), [1, 2]),
            _table(
                "Average per crop",
                ["Crop", "Farmers", "Average score"],
                sorted(([c, len(v), round_int(_avg(v))] for c, v in by_crop.items()), key=lambda r: -r[1]),
                [1, 2],
            ),
        ],
    }


def _duration(minutes: float) -> str:
    """"2 h 15 min", "3.0 days"."""
    if minutes <= 0:
        return "—"
    if minutes < 60:
        return f"{round_int(minutes)} min"
    if minutes < 48 * 60:
        return f"{int(minutes // 60)} h {round_int(minutes % 60)} min"
    return f"{minutes / 1440:.1f} days"


def _verifications(d: ReportData, now: datetime) -> dict:
    every = d.verifications

    def review_minutes(items: list[VerificationRequest]) -> float:
        # Instant checks are approved automatically; review time counts people's reviews only.
        reviewed = [
            v for v in items if v.status != "pending" and v.reviewed_at and v.reviewed_by != "Automatic check"
        ]
        return _avg([(v.reviewed_at - v.submitted_at).total_seconds() / 60 for v in reviewed])

    def rate(items: list[VerificationRequest]) -> float | None:
        approved = sum(1 for v in items if v.status == "approved")
        rejected = sum(1 for v in items if v.status == "rejected")
        return approved / (approved + rejected) if approved + rejected else None

    rejected = [v for v in every if v.status == "rejected"]
    reasons = list(dict.fromkeys(v.reason or "No reason" for v in rejected))
    return {
        "summary": [
            ("Requests", str(len(every))),
            ("Pending", str(sum(1 for v in every if v.status == "pending"))),
            ("Approval rate", _pct_label(rate(every))),
            ("Average review time", _duration(review_minutes(every))),
        ],
        "tables": [
            _table(
                "By check",
                ["Check", "Requests", "Pending", "Approved", "Rejected", "Approval rate", "Average review time"],
                [
                    [
                        label,
                        len(items),
                        sum(1 for v in items if v.status == "pending"),
                        sum(1 for v in items if v.status == "approved"),
                        sum(1 for v in items if v.status == "rejected"),
                        _pct_label(rate(items)),
                        _duration(review_minutes(items)),
                    ]
                    for kind, label in KIND_LABELS.items()
                    for items in [[v for v in every if v.kind == kind]]
                ],
                [1, 2, 3, 4, 5],
            ),
            _table(
                "Rejection reasons",
                ["Reason", "Rejected"],
                sorted(
                    ([r, sum(1 for v in rejected if (v.reason or "No reason") == r)] for r in reasons),
                    key=lambda row: -row[1],
                ),
                [1],
            ),
        ],
    }


def _risk(d: ReportData, now: datetime) -> dict:
    flags = d.risk_flags

    def count(items: list[RiskFlag], status: str) -> int:
        return sum(1 for f in items if f.status == status)

    return {
        "summary": [
            ("Flags", str(len(flags))),
            ("Open", str(count(flags, "open"))),
            ("Confirmed fraud", str(count(flags, "confirmed"))),
            ("Dismissed", str(count(flags, "dismissed"))),
            ("Accounts confirmed", str(len({f.user_id for f in flags if f.status == "confirmed"}))),
        ],
        "tables": [
            _table(
                "By rule and outcome",
                ["Rule", "Severity", "Flags", "Open", "Confirmed", "Dismissed"],
                [
                    [rule.label, SEVERITY_LABELS[rule.severity], len(items), count(items, "open"), count(items, "confirmed"), count(items, "dismissed")]
                    for rule in RISK_RULES
                    for items in [[f for f in flags if f.rule == rule.id]]
                ],
                [2, 3, 4, 5],
            )
        ],
    }


_BUILDERS = {
    "platform": _platform,
    "regions": _regions,
    "loans": _loans,
    "credit": _credit,
    "verifications": _verifications,
    "risk": _risk,
}


def build_report(report_id: str, data: ReportData, now: datetime) -> dict:
    meta = next(r for r in REPORT_CATALOG if r["id"] == report_id)
    body = _BUILDERS[report_id](data, now)
    return {
        **meta,
        "generatedAt": now,
        "summary": [{"label": label, "value": value} for label, value in body["summary"]],
        "tables": body["tables"],
    }


def report_to_csv(report: dict) -> str:
    """The headline figures, then each table under its title."""
    generated = report["generatedAt"]
    rows: list[list[Any]] = [
        [f"AgriCtec — {report['title']}"],
        [f"Generated {generated.day} {month_label(generated.date())[:3]} {generated.year}, {generated:%H:%M}"],
        [],
        ["Summary"],
        *[[s["label"], s["value"]] for s in report["summary"]],
    ]
    for table in report["tables"]:
        rows += [[], [table["title"]], table["headers"], *table["rows"]]
    return rows_to_csv(rows)


def report_file_name(report: dict, ext: str) -> str:
    title = re.sub(r"\W+", "_", report["title"])
    return f"AgriCtec_{title}_{iso(report['generatedAt'])[:10]}.{ext}"


# ---------- The custom report builder ----------


@dataclass
class Column:
    key: str
    label: str
    value: Callable[[Any], Any]
    default: bool = False


@dataclass
class Located:
    row: Any
    region: str
    crops: list[str]
    date: str


def _farmer_rows(d: ReportData) -> list[Located]:
    return [
        Located(f, f.profile.region, crop_names(f.profile), as_date(f.created_at).isoformat())
        for f in d.farmers
        if not f.is_guest
    ]


def _owner_rows(items, d: ReportData, crops_of: Callable[[Any, FarmerAccount], list[str]]) -> list[Located]:
    farmers = {f.user_id: f for f in d.farmers}
    out = []
    for item in items:
        f = farmers.get(item.user_id)
        if not f or f.is_guest:
            continue
        out.append(Located((item, f.profile.name or f.email), f.profile.region, crops_of(item, f), item.date.isoformat()))
    return out


def _application_rows(d: ReportData) -> list[Located]:
    farmers = {f.user_id: f for f in d.farmers}
    lenders = {lender.id: lender.name for lender in d.lenders}
    out = []
    for a in d.applications:
        f = farmers.get(a.user_id)
        out.append(
            Located(
                (a, (f.profile.name or f.email) if f else "Deleted farmer", lenders.get(a.partner_id, "Unknown lender")),
                f.profile.region if f else "",
                crop_names(f.profile) if f else [],
                as_date(a.created_at).isoformat(),
            )
        )
    return out


def _size(f: FarmerAccount) -> str:
    unit = f.profile.size_unit
    value = round(f.profile.acres / 2.47105 if unit == "hectares" else f.profile.acres, 2)
    name = "hectare" if unit == "hectares" else "acre"
    text = str(int(value)) if float(value).is_integer() else f"{value:,}"
    return f"{text} {name}{'' if value == 1 else 's'}"


DATASETS: dict[str, dict] = {
    "farmers": {
        "label": "Farmers",
        "description": "One row per farmer account (guests left out).",
        "dateLabel": "Joined",
        "rows": _farmer_rows,
        "columns": [
            Column("name", "Name", lambda f: f.profile.name, True),
            Column("email", "Email", lambda f: f.email),
            Column("phone", "Phone", lambda f: f.profile.phone),
            Column("region", "Region", lambda f: f.profile.region, True),
            Column("constituency", "Constituency", lambda f: f.profile.constituency),
            Column("farm", "Farm", lambda f: f.profile.farm_name),
            Column("farmType", "Farm type", lambda f: f.profile.farm_type),
            Column("size", "Farm size", _size),
            Column("crops", "Crops", lambda f: "; ".join(crop_names(f.profile)), True),
            Column("score", "Credit score", lambda f: f.stats.score, True),
            Column("income", "Income (GHS)", lambda f: f.stats.income, True),
            Column("expenses", "Expenses (GHS)", lambda f: f.stats.expenses),
            Column("records", "Records", lambda f: f.stats.records),
            Column("momo", "MoMo transactions", lambda f: f.stats.momo),
            Column("card", "Ghana Card", lambda f: GHANA_CARD_LABELS[f.profile.ghana_card.status], True),
            Column("status", "Status", lambda f: f.status),
            Column("joined", "Joined", lambda f: as_date(f.created_at).isoformat(), True),
        ],
    },
    "records": {
        "label": "Farm records",
        "description": "Every income, expense and activity record.",
        "dateLabel": "Record date",
        "rows": lambda d: _owner_rows(d.records, d, lambda r, f: [r.crop]),
        "columns": [
            Column("date", "Date", lambda x: x[0].date.isoformat(), True),
            Column("farmer", "Farmer", lambda x: x[1], True),
            Column("type", "Type", lambda x: x[0].type, True),
            Column("crop", "Crop", lambda x: x[0].crop, True),
            Column("description", "Description", lambda x: x[0].description, True),
            Column("amount", "Amount (GHS)", lambda x: x[0].amount, True),
            Column("entered", "Entered", lambda x: iso(x[0].created_at)[:16].replace("T", " ")),
        ],
    },
    "momo": {
        "label": "MoMo transactions",
        "description": "Every Mobile Money transaction farmers logged.",
        "dateLabel": "Transaction date",
        "rows": lambda d: _owner_rows(d.momo, d, lambda t, f: crop_names(f.profile)),
        "columns": [
            Column("date", "Date", lambda x: x[0].date.isoformat(), True),
            Column("farmer", "Farmer", lambda x: x[1], True),
            Column("direction", "Direction", lambda x: "In" if x[0].direction == "in" else "Out", True),
            Column("counterparty", "Counterparty", lambda x: x[0].counterparty, True),
            Column("amount", "Amount (GHS)", lambda x: x[0].amount, True),
            Column("reference", "Reference", lambda x: x[0].reference),
            Column("network", "Network", lambda x: x[0].network or ""),
        ],
    },
    "applications": {
        "label": "Loan applications",
        "description": "Every loan application with its current status.",
        "dateLabel": "Sent",
        "rows": _application_rows,
        "columns": [
            Column("reference", "Reference", lambda x: x[0].reference, True),
            Column("farmer", "Farmer", lambda x: x[1], True),
            Column("lender", "Lender", lambda x: x[2], True),
            Column("amount", "Amount (GHS)", lambda x: x[0].amount, True),
            Column("purpose", "Purpose", lambda x: x[0].purpose),
            Column("months", "Months", lambda x: x[0].months),
            Column("rate", "Rate (%)", lambda x: x[0].rate),
            Column("score", "Score at application", lambda x: x[0].score_at_application),
            Column("status", "Status", lambda x: LOAN_STATUS_LABELS[x[0].status], True),
            Column("sent", "Sent", lambda x: as_date(x[0].created_at).isoformat(), True),
        ],
    },
}


def datasets_meta() -> list[dict]:
    return [
        {
            "id": key,
            "label": ds["label"],
            "description": ds["description"],
            "dateLabel": ds["dateLabel"],
            "columns": [{"key": c.key, "label": c.label, "default": c.default} for c in ds["columns"]],
        }
        for key, ds in DATASETS.items()
    ]


def run_builder(spec, data: ReportData) -> dict:
    """Applies the filters and picks the columns (in the dataset's order), newest first."""
    ds = DATASETS[spec.dataset]
    columns = [c for c in ds["columns"] if c.key in spec.columns]
    crop = spec.crop.lower() if spec.crop else None
    start = spec.from_.isoformat() if spec.from_ else None
    end = spec.to.isoformat() if spec.to else None
    rows = [
        item
        for item in ds["rows"](data)
        if (not spec.region or item.region == spec.region)
        and (not crop or any(c.lower() == crop for c in item.crops))
        and (not start or item.date >= start)
        and (not end or item.date <= end)
    ]
    rows.sort(key=lambda item: item.date, reverse=True)
    return {
        "headers": [c.label for c in columns],
        "rows": [[c.value(item.row) for c in columns] for item in rows],
        "total": len(rows),
    }


def builder_to_csv(result: dict) -> str:
    return to_csv(
        result["rows"],
        [(header, (lambda i: lambda row: row[i])(i)) for i, header in enumerate(result["headers"])],
    )
