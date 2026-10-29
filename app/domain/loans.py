"""Loan maths and eligibility (lib/calculations/loan.ts) and the pipeline (lib/admin/loan-*.ts)."""

import re
from datetime import date, datetime

from app.domain.clock import as_date
from app.domain.finance import annualized_net_profit
from app.domain.fmt import format_ghs, js_round, round_int
from app.schemas.farm import (
    EligibilityIssue,
    EligibilityResult,
    FarmRecord,
    LoanApplication,
    RepaymentResult,
)
from app.schemas.platform import CreditEligibility, Lender

# ---------- Farmer side ----------

LOANS_LOCKED_HINT = "Verify your Ghana Card to unlock loan offers."


def borrowing_limit(annualized_profit: float, rules: CreditEligibility) -> float:
    """The most a farmer can borrow: the admin's multiple of their annualized profit."""
    return max(0, rules.max_profit_multiplier * annualized_profit)


def monthly_repayment(principal: float, annual_rate_pct: float, months: int) -> RepaymentResult:
    """Standard amortized repayment: equal monthly payments covering principal and interest."""
    if principal <= 0 or months <= 0:
        return RepaymentResult(monthly=0, total=0, interest=0)
    r = annual_rate_pct / 100 / 12
    monthly = principal / months if r == 0 else principal * r / (1 - (1 + r) ** -months)
    total = monthly * months
    return RepaymentResult(
        monthly=js_round(monthly, 2), total=js_round(total, 2), interest=js_round(total - principal, 2)
    )


def ghana_card_loan_message(status: str) -> str:
    return (
        "Your Ghana Card is being reviewed. You can apply once it is verified."
        if status == "pending"
        else "Verify your Ghana Card to apply for loans."
    )


def check_eligibility(
    *,
    amount: float,
    records: list[FarmRecord],
    score: int,
    rules: CreditEligibility,
    partner: Lender | None = None,
    ghana_card: str = "verified",
) -> EligibilityResult:
    """Every failing rule, not just the first. A chosen lender's own minimum score replaces the
    platform minimum; the platform minimum applies when no lender is chosen."""
    issues: list[EligibilityIssue] = []
    max_affordable = borrowing_limit(annualized_net_profit(records), rules)
    min_score = partner.min_score if partner else rules.min_score

    if ghana_card != "verified":
        issues.append(EligibilityIssue(code="ghana-card", message=ghana_card_loan_message(ghana_card)))
    if len(records) < rules.min_records:
        issues.append(
            EligibilityIssue(
                code="records",
                message=f"You need at least {rules.min_records} farm records (you have {len(records)}).",
            )
        )
    if amount > max_affordable:
        multiple = _number(rules.max_profit_multiplier)
        issues.append(
            EligibilityIssue(
                code="affordability",
                message=f"Requested amount is above {multiple}× your yearly profit (max {format_ghs(max_affordable)}).",
            )
        )
    if score < min_score:
        issues.append(
            EligibilityIssue(
                code="score", message=f"Credit score must be at least {min_score} (yours is {score})."
            )
        )
    if partner and amount > partner.max_amount:
        issues.append(
            EligibilityIssue(
                code="lender-max",
                message=f"Above this lender's maximum of {format_ghs(partner.max_amount)}.",
            )
        )
    return EligibilityResult(
        eligible=not issues,
        reasons=[i.message for i in issues],
        issues=issues,
        max_affordable=max_affordable,
    )


def match_percentage(score: int, partner: Lender) -> int:
    """How well the farmer's score matches a lender, from 40% to 98%."""
    return round_int(min(98, max(40, 70 + (score - partner.min_score) / 5)))


def _number(value: float) -> str:
    """3.0 -> "3", 2.5 -> "2.5" (as JavaScript prints numbers)."""
    return str(int(value)) if float(value).is_integer() else str(value)


# ---------- The pipeline ----------

LOAN_STATUSES = ["submitted", "under_review", "approved", "rejected", "disbursed", "repaid", "defaulted"]
LOAN_STATUS_LABELS = {
    "submitted": "Submitted",
    "under_review": "Under review",
    "approved": "Approved",
    "rejected": "Rejected",
    "disbursed": "Disbursed",
    "repaid": "Repaid",
    "defaulted": "Defaulted",
}
# The only allowed moves. Rejected, repaid and defaulted are final.
LOAN_TRANSITIONS: dict[str, list[str]] = {
    "submitted": ["under_review", "rejected"],
    "under_review": ["approved", "rejected"],
    "approved": ["disbursed", "rejected"],
    "disbursed": ["repaid", "defaulted"],
    "rejected": [],
    "repaid": [],
    "defaulted": [],
}
# Once the farmer's account is deleted, an application can only be closed out.
CLOSING_STATUSES = ["rejected", "repaid", "defaulted"]
NOTE_REQUIRED_STATUSES = ["approved", "rejected", "defaulted"]
DISBURSED_LOAN_STATUSES = ["disbursed", "repaid", "defaulted"]
APPROVED_LOAN_STATUSES = ["approved", "disbursed", "repaid", "defaulted"]
DELETED_FARMER_LABEL = "Farmer deleted"


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in LOAN_TRANSITIONS.get(from_status, [])


def status_change_error(
    app: LoanApplication,
    to: str,
    *,
    note: str | None,
    disbursed_on: str | None,
    disbursement_ref: str | None,
    now: datetime,
    farmer_deleted: bool,
) -> str | None:
    """The first rule a move breaks (lib/admin/loan-pipeline.ts statusChangeSchema), or None."""
    if not can_transition(app.status, to):
        return f"An application can’t move from {LOAN_STATUS_LABELS[app.status]} to {LOAN_STATUS_LABELS[to]}."
    if farmer_deleted and to not in CLOSING_STATUSES:
        return f"This farmer’s account was deleted, so the application can only be closed, not moved to {LOAN_STATUS_LABELS[to]}."
    if to in NOTE_REQUIRED_STATUSES and len((note or "").strip()) < 3:
        return f"Add a note: the farmer sees why it was {LOAN_STATUS_LABELS[to].lower()}."
    if to == "disbursed":
        value = disbursed_on or ""
        try:
            day = date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
        except ValueError:
            day = None
        if day is None:
            return "Enter the disbursement date."
        if day > now.date():
            return "The date can’t be in the future."
        if day < as_date(app.created_at):
            return "The date can’t be before the application was sent."
        if len((disbursement_ref or "").strip()) < 3:
            return "Enter the transfer reference (at least 3 characters)."
    return None


def loan_stats(apps: list[LoanApplication]) -> dict:
    def total(statuses: list[str]) -> float:
        return sum(a.amount for a in apps if a.status in statuses)

    repaid = sum(1 for a in apps if a.status == "repaid")
    defaulted = sum(1 for a in apps if a.status == "defaulted")
    closed = repaid + defaulted
    return {
        "totalRequested": sum(a.amount for a in apps),
        "approvedAmount": total(APPROVED_LOAN_STATUSES),
        "disbursedAmount": total(DISBURSED_LOAN_STATUSES),
        "defaultRate": defaulted / closed if closed else None,
        "closed": closed,
    }


def lender_stats(apps: list, lender_id: str) -> dict:
    mine = [a for a in apps if a.partner_id == lender_id]
    approved = sum(1 for a in mine if a.status in APPROVED_LOAN_STATUSES)
    rejected = sum(1 for a in mine if a.status == "rejected")
    return {
        "applications": len(mine),
        "underReview": sum(1 for a in mine if a.status == "under_review"),
        "approvalRate": approved / (approved + rejected) if approved + rejected else None,
    }


def deactivate_block(lender: Lender, apps: list) -> str | None:
    """Why a lender can't be switched off right now (None = it can)."""
    if not lender.active:
        return None
    reviewing = lender_stats(apps, lender.id)["underReview"]
    if not reviewing:
        return None
    them = "it" if reviewing == 1 else "them"
    return (
        f"{lender.name} has {reviewing} application{'' if reviewing == 1 else 's'} under review. "
        f"Approve or reject {them} first, so no farmer is left waiting on a hidden lender."
    )


def delete_block(lender: Lender, apps: list) -> str | None:
    """Why a lender can't be deleted (None = it can): any application keeps its history."""
    count = lender_stats(apps, lender.id)["applications"]
    if not count:
        return None
    return (
        f"{lender.name} has {count} application{'' if count == 1 else 's'} on record, and deleting "
        "it would break their history. Deactivate it instead."
    )


def eligibility_checks(
    *, amount: float, months: int, records: list[FarmRecord], score: int, lender: Lender | None, rules: CreditEligibility
) -> list[dict]:
    """Every rule the farmer app applies, as pass/fail lines with today's rules."""
    limit = borrowing_limit(annualized_net_profit(records), rules)
    min_score = lender.min_score if lender else rules.min_score
    checks = [
        {
            "key": "records",
            "label": f"At least {rules.min_records} farm records",
            "pass": len(records) >= rules.min_records,
            "detail": f"{len(records)} on file",
        },
        {
            "key": "affordability",
            "label": f"Within {_number(rules.max_profit_multiplier)}× yearly profit",
            "pass": amount <= limit,
            "detail": f"Limit {format_ghs(limit)}",
        },
        {
            "key": "score",
            "label": f"Credit score {min_score}+",
            "pass": score >= min_score,
            "detail": f"Now {score}",
        },
    ]
    if lender:
        checks += [
            {
                "key": "lender-max",
                "label": f"{lender.name} lends up to {format_ghs(lender.max_amount)}",
                "pass": amount <= lender.max_amount,
                "detail": f"Asked {format_ghs(amount)}",
            },
            {
                "key": "term",
                "label": f"Term of {lender.min_months}–{lender.max_months} months",
                "pass": lender.min_months <= months <= lender.max_months,
                "detail": f"{months} months",
            },
        ]
    return checks
