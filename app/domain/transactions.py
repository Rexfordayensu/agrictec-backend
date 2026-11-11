"""Transaction provenance and status rules. No provider integration exists."""

from app.core.errors import AppError, Forbidden

STATUSES = ("self_reported", "evidence_submitted", "verification_pending", "verified", "rejected", "disputed")
SOURCES = ("farmer_entry", "statement_upload")
# provider_integration and payment_integration are reserved, unavailable sources.
CATEGORIES = {
    "income": ("crop_sale", "livestock_sale", "produce_delivery", "other_farm_income"),
    "expense": (
        "fertilizer",
        "seed",
        "equipment",
        "transport",
        "labour",
        "irrigation",
        "land_rent",
        "other_farm_expense",
    ),
    "financing": ("loan_disbursement", "loan_repayment", "interest_payment", "other_financing"),
    "activity": (),
}
ALL_CATEGORIES = tuple(c for values in CATEGORIES.values() for c in values)
MATERIAL_FIELDS = {
    "amount",
    "date",
    "type",
    "category",
    "direction",
    "reference",
    "external_reference",
    "payment_method",
    "source_type",
    "counterparty",
    "description",
    "crop",
    "network",
}


def transition(status: str | None, action: str, *, evidence_count: int = 0, reason: str | None = None) -> str:
    if action == "create" and status is None:
        return "self_reported"
    if status not in STATUSES:
        raise AppError("Unknown transaction status.")
    if action == "attach":
        if status == "verified":
            raise Forbidden("Dispute a verified entry before adding new evidence.")
        return "evidence_submitted" if status in ("self_reported", "rejected") else status
    if action == "edit":
        if status == "verified":
            raise Forbidden("Verified entries cannot be edited or deleted. Add a note or dispute the entry.")
        return "self_reported"
    if action == "request_review" and status in ("self_reported", "evidence_submitted", "rejected"):
        if not evidence_count:
            raise AppError("Attach at least one evidence file before requesting a review.")
        return "verification_pending"
    if action in ("verify", "reject") and status in ("verification_pending", "disputed"):
        if not reason or not reason.strip():
            raise AppError("Give a reason for the review decision.")
        return "verified" if action == "verify" else "rejected"
    if action == "dispute" and status in ("rejected", "verified"):
        if not reason or not reason.strip():
            raise AppError("Give a reason for the dispute.")
        return "disputed"
    if action == "remove_evidence":
        if status in ("verified", "verification_pending"):
            raise Forbidden("Evidence linked to a verified entry or an entry under review cannot be removed.")
        return "self_reported" if evidence_count == 0 and status == "evidence_submitted" else status
    raise AppError("That action is not allowed from the entry's current status.", code="invalid_transition")


def validate_fields(kind: str, values: dict) -> None:
    category = values.get("category")
    if category and category not in (CATEGORIES.get(values.get("type"), ()) if kind == "farm" else ALL_CATEGORIES):
        raise AppError("Choose a category that matches the entry type.")
    if kind == "farm":
        if values.get("type") == "financing" and values.get("direction") not in ("in", "out"):
            raise AppError("Financing entries need an in or out direction.")
        if values.get("type") != "financing" and values.get("direction") is not None:
            raise AppError("Direction is only used for financing records.")
        if values.get("type") == "activity" and (category or values.get("amount", 0) != 0):
            raise AppError("Activity records have no category or amount.")
