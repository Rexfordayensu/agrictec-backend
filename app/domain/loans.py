"""Loan maths and eligibility (lib/calculations/loan.ts) and the pipeline (lib/admin/loan-*.ts)."""

import re
from datetime import date, datetime

from app.domain.clock import as_date
from app.domain.fmt import js_round
from app.schemas.farm import (
    EligibilityIssue,
    EligibilityResult,
    FarmRecord,
    LoanApplication,
    RepaymentResult,
)
from app.schemas.platform import CreditEligibility

# ---------- Farmer side ----------

LOANS_LOCKED_HINT = "Verify your Ghana Card before you apply to a lender."


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


def check_eligibility(*, ghana_card: str = "verified") -> EligibilityResult:
    """The identity check AgriCtec itself makes before any application: a verified Ghana Card.
    The AgriCtec score and AgriCtec's own limits never decide a loan; each lender's published
    product requirements do (checked per product, not here)."""
    issues: list[EligibilityIssue] = []
    if ghana_card != "verified":
        issues.append(EligibilityIssue(code="ghana-card", message=ghana_card_loan_message(ghana_card)))
    return EligibilityResult(eligible=not issues, reasons=[i.message for i in issues], issues=issues)


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


def delete_block(name: str, applications: int) -> str | None:
    """Why a lender can't be deleted (None = it can): any application keeps its history."""
    count = applications
    if not count:
        return None
    return (
        f"{name} has {count} application{'' if count == 1 else 's'} on record, and deleting "
        "it would break their history. Unpublish or suspend it instead."
    )


def eligibility_checks(*, records: list[FarmRecord], ghana_card: str, rules: CreditEligibility) -> list[dict]:
    """What AgriCtec itself checks, with today's data: the Ghana Card, and its record-keeping
    guidance (a target, not a lender rule). The score is shown elsewhere, never as a pass/fail."""
    return [
        {
            "key": "ghana-card",
            "label": "Ghana Card verified",
            "pass": ghana_card == "verified",
            "detail": ghana_card.replace("_", " "),
        },
        {
            "key": "records",
            "label": f"AgriCtec guidance: {rules.min_records}+ farm records",
            "pass": len(records) >= rules.min_records,
            "detail": f"{len(records)} on file",
        },
    ]
