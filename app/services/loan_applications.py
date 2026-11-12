"""Loan applications (milestone 6): prepared by the farmer, reviewed by AgriCtec, sent to the
lender, and followed with the lender's own confirmation.

Flow:
  draft -> submitted (the farmer consents to sharing; the eligibility check and the transaction
  statement are attached) -> in_review (AgriCtec checks it tallies with the farmer's records)
  -> changes_requested (back to the farmer) or approved_for_sending -> sent_to_lender (by email
  to the contact the lender agreed to, or recorded by an administrator who delivered it) ->
  received_by_lender / under_lender_review / documents_requested / approved_by_lender /
  rejected_by_lender (each only with the lender's own confirmation attached). The farmer can
  withdraw at any time before the lender's decision.

Never: an AgriCtec loan approval, a lender status without the lender's proof, or sharing
without the farmer's consent. A lender's confirmed mandatory condition that is not met (the
eligibility check says "does not meet") stops submission; incomplete history never does.
"""

import hashlib
import re
import secrets
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.audit import append_audit
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.domain.application_pack import render as render_pack
from app.domain.clock import iso, now_utc, today
from app.domain.loan_assessment import analyse, entries_from
from app.integrations import messaging
from app.integrations.storage import store
from app.models import (
    Admin,
    FarmerLoanApplication,
    FarmRecord,
    FinancialReportExport,
    Lender,
    LoanApplicationDocument,
    LoanApplicationEvent,
    LoanDataSharingConsent,
    LoanProduct,
    MomoTransaction,
)
from app.services import farm, financial_reports, lending, loan_eligibility, loan_products, notifications, platform_store
from app.services import ghana_card_verification as identity
from app.services.transactions import clean_file

STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Submitted to AgriCtec",
    "in_review": "AgriCtec is reviewing",
    "changes_requested": "Changes requested",
    "approved_for_sending": "Ready to send to the lender",
    "sent_to_lender": "Sent to the lender",
    "received_by_lender": "Received by the lender",
    "under_lender_review": "Under the lender's review",
    "documents_requested": "The lender asked for more documents",
    "approved_by_lender": "Approved by the lender",
    "rejected_by_lender": "Not approved by the lender",
    "withdrawn": "Withdrawn",
}
EDITABLE = ("draft", "changes_requested")
FINAL = ("approved_by_lender", "rejected_by_lender", "withdrawn")
REVIEWABLE = ("submitted", "in_review")
LENDER_STATUSES = ("received_by_lender", "under_lender_review", "documents_requested",
                   "approved_by_lender", "rejected_by_lender")
WITH_LENDER = ("sent_to_lender", "received_by_lender", "under_lender_review", "documents_requested")

CONSENT_VERSION = "2026-11-12"
DATA_ITEMS = [
    "Your name, phone number and farm details",
    "This application: the amount, period, purpose, your letter and your answers",
    "The documents you uploaded for this application",
    "Your transaction statement for the period the lender assesses (each entry marked verified or self-reported)",
    "AgriCtec's preliminary eligibility check for this product",
]
MAX_DOCUMENTS_PER_REQUIREMENT = 5
LETTER_LIMIT = 4000
GROUPS = {
    "to_review": ("submitted", "in_review"),
    "ready_to_send": ("approved_for_sending",),
    "with_lender": WITH_LENDER,
    "returned": ("changes_requested",),
    "closed": FINAL,
}


# ---------- Helpers ----------


def _reference(db: DB) -> str:
    while True:
        ref = f"APP-{today():%Y}-{secrets.token_hex(3).upper()}"
        if not db.scalar(select(FarmerLoanApplication.id).where(FarmerLoanApplication.reference == ref)):
            return ref


def _event(db: DB, app: FarmerLoanApplication, status: str, actor_type: str, actor_name: str,
           note: str | None = None, file_key: str | None = None, file_name: str | None = None) -> None:
    db.add(LoanApplicationEvent(application_id=app.id, status=status, actor_type=actor_type,
                                actor_name=actor_name[:120], note=(note or None) and note[:1000],
                                file_key=file_key, file_name=file_name, at=now_utc()))


def _set_status(db: DB, app: FarmerLoanApplication, status: str, actor_type: str, actor_name: str,
                note: str | None = None, **file) -> None:
    app.status, app.updated_at = status, now_utc()
    _event(db, app, status, actor_type, actor_name, note, **file)


def _notify(db: DB, app: FarmerLoanApplication, title: str, body: str) -> None:
    row = notifications.create(db, app.user_id, "loan", title, body, href=f"/loans/applications/{app.id}")
    db.flush()
    notifications.deliver(row)


def _requirements(app: FarmerLoanApplication) -> list[dict]:
    return list((app.product_snapshot or {}).get("requirements") or [])


def _visible(req: dict, answers: dict) -> bool:
    cond = req.get("visible_if")
    return cond is None or str(answers.get(cond["key"], "")) == str(cond["equals"])


def _docs(db: DB, app_id: str) -> list[LoanApplicationDocument]:
    return list(db.scalars(select(LoanApplicationDocument)
                           .where(LoanApplicationDocument.application_id == app_id)
                           .order_by(LoanApplicationDocument.uploaded_at)))


def missing_items(app: FarmerLoanApplication, docs: list[LoanApplicationDocument]) -> list[str]:
    """What the farmer still has to provide before submitting."""
    have_docs = {d.requirement_key for d in docs}
    missing = []
    for req in _requirements(app):
        kind = req.get("field_type", "none")
        if kind == "none" or not req.get("required", True) or not _visible(req, app.answers):
            continue
        if kind == "document":
            if req["key"] not in have_docs:
                missing.append(f"Upload: {req['name']}")
        elif str(app.answers.get(req["key"], "")).strip() == "":
            missing.append(f"Answer: {req['name']}")
    if len((app.letter or "").strip()) < 30:
        missing.append("Write your application letter (at least 30 characters).")
    return missing


def _clean_answer(req: dict, value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    kind, text = req.get("field_type"), str(value).strip()
    name = req["name"]
    if kind == "yes_no":
        if text not in ("yes", "no"):
            raise AppError(f"{name}: answer yes or no.")
    elif kind == "select":
        if text not in (req.get("options") or []):
            raise AppError(f"{name}: choose one of the listed options.")
    elif kind in ("number", "amount"):
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise AppError(f"{name}: enter a number.") from None
        lo, hi = req.get("min"), req.get("max")
        if lo is not None and number < Decimal(str(lo)):
            raise AppError(f"{name}: at least {lo}.")
        if hi is not None and number > Decimal(str(hi)):
            raise AppError(f"{name}: at most {hi}.")
    elif kind == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise AppError(f"{name}: enter a date.")
    elif kind == "text":
        if len(text) > 1000:
            raise AppError(f"{name}: keep it under 1,000 characters.")
    else:
        raise AppError(f"{name} is not something you answer.")
    return text


def _own(db: DB, user_id: str, app_id: str, *, lock: bool = False) -> FarmerLoanApplication:
    query = select(FarmerLoanApplication).where(FarmerLoanApplication.id == app_id)
    app = db.scalars(query.with_for_update() if lock else query).first()
    if app is None or app.user_id != user_id:
        raise NotFound("Application not found.")
    return app


def _app(db: DB, app_id: str, *, lock: bool = False) -> FarmerLoanApplication:
    query = select(FarmerLoanApplication).where(FarmerLoanApplication.id == app_id)
    app = db.scalars(query.with_for_update() if lock else query).first()
    if app is None or app.status == "draft":
        raise NotFound("Application not found.")
    return app


def _consent(db: DB, app_id: str) -> LoanDataSharingConsent | None:
    return db.scalars(select(LoanDataSharingConsent).where(LoanDataSharingConsent.application_id == app_id)
                      .order_by(LoanDataSharingConsent.given_at.desc())).first()


# ---------- Output ----------


def out(db: DB, app: FarmerLoanApplication, *, for_admin: bool = False) -> dict:
    docs = _docs(db, app.id)
    by_key: dict[str, list[dict]] = {}
    for d in docs:
        by_key.setdefault(d.requirement_key, []).append({
            "id": d.id, "filename": d.filename, "contentType": d.content_type, "sizeBytes": d.size_bytes,
            "uploadedAt": iso(d.uploaded_at),
        })
    requirements = []
    for req in _requirements(app):
        requirements.append({
            "key": req["key"], "name": req["name"], "description": req.get("description"),
            "category": req.get("category"), "required": req.get("required", True),
            "fieldType": req.get("field_type", "none"), "options": req.get("options") or [],
            "min": req.get("min"), "max": req.get("max"), "allowedFormats": req.get("allowed_formats") or [],
            "maxSizeMb": req.get("max_size_mb"), "verification": req.get("verification"),
            "visible": _visible(req, app.answers), "answer": app.answers.get(req["key"]),
            "documents": by_key.get(req["key"], []), "checkedByAgriCtec": req.get("check") is not None,
        })
    events = db.scalars(select(LoanApplicationEvent).where(LoanApplicationEvent.application_id == app.id)
                        .order_by(LoanApplicationEvent.at, LoanApplicationEvent.id)).all()
    consent = _consent(db, app.id)
    lender = db.get(Lender, app.lender_id)
    missing = missing_items(app, docs) if app.status in EDITABLE else []
    assessment = app.assessment_snapshot
    data = {
        "id": app.id, "reference": app.reference, "status": app.status, "statusLabel": STATUS_LABELS[app.status],
        "lender": {"id": app.lender_id, "name": lender.name if lender else None,
                   "canReceive": bool(lender and lender.submission_email and lender.submission_agreed_on)},
        "product": {"id": app.product_id, "name": (app.product_snapshot or {}).get("name"),
                    "version": app.product_version, "terms": (app.product_snapshot or {}).get("terms")},
        "amount": f"{app.amount:.2f}", "months": app.months, "purpose": app.purpose, "letter": app.letter,
        "requirements": requirements,
        "missing": missing,
        "assessment": None if not assessment else {
            "outcome": assessment.get("outcome"), "outcomeLabel": assessment.get("outcomeLabel"),
            "summary": assessment.get("summary"), "period": assessment.get("period"),
            "figures": assessment.get("figures"), "rules": assessment.get("rules"),
            "createdAt": assessment.get("createdAt"),
        },
        "statementReportId": app.statement_report_id,
        "consent": None if not consent else {
            "purpose": consent.purpose, "dataItems": consent.data_items, "noticeVersion": consent.notice_version,
            "givenAt": iso(consent.given_at), "withdrawnAt": iso(consent.withdrawn_at) if consent.withdrawn_at else None,
        },
        "timeline": [{
            "status": e.status, "label": STATUS_LABELS.get(e.status, e.status),
            "by": e.actor_name if for_admin else _farmer_sees_actor(e),
            "actorType": e.actor_type, "lenderConfirmed": e.status in LENDER_STATUSES,
            "note": e.note, "at": iso(e.at), "eventId": e.id,
            "hasProof": e.file_key is not None, "proofName": e.file_name,
        } for e in events],
        "reviewNote": app.review_note,
        "submittedAt": iso(app.submitted_at) if app.submitted_at else None,
        "sentAt": iso(app.sent_at) if app.sent_at else None, "sentVia": app.sent_via,
        "canEdit": app.status in EDITABLE,
        "canSubmit": app.status in EDITABLE and not missing,
        "canWithdraw": app.status not in FINAL,
        "createdAt": iso(app.created_at), "updatedAt": iso(app.updated_at),
    }
    if for_admin:
        data["userId"] = app.user_id
        data["sentTo"] = app.sent_to
    return data


def _farmer_sees_actor(e: LoanApplicationEvent) -> str:
    """Farmers see "You" or "AgriCtec", never a staff member's name."""
    if e.actor_type == "farmer":
        return "You"
    if e.actor_type == "admin":
        return "AgriCtec, confirmed by the lender" if e.status in LENDER_STATUSES else "AgriCtec"
    return e.actor_name


def summary_out(db: DB, app: FarmerLoanApplication) -> dict:
    lender = db.get(Lender, app.lender_id)
    return {
        "id": app.id, "reference": app.reference, "status": app.status, "statusLabel": STATUS_LABELS[app.status],
        "lenderName": lender.name if lender else None, "productName": (app.product_snapshot or {}).get("name"),
        "amount": f"{app.amount:.2f}", "months": app.months, "submittedAt": iso(app.submitted_at) if app.submitted_at else None,
        "updatedAt": iso(app.updated_at), "createdAt": iso(app.created_at),
    }


# ---------- Farmer ----------


def _loans_on(db: DB) -> None:
    if not platform_store.flags(db).loans:
        raise Forbidden("Loans are currently unavailable.", code="feature_off")


def create(db: DB, user_id: str, product_id: str, amount: Decimal, months: int, purpose: str) -> dict:
    _loans_on(db)
    product = loan_products.listed_product(db, product_id)  # 404 unless farmers can see it
    open_one = db.scalars(select(FarmerLoanApplication).where(
        FarmerLoanApplication.user_id == user_id, FarmerLoanApplication.product_id == product_id,
        FarmerLoanApplication.status.not_in(FINAL),
    )).first()
    if open_one is not None:
        raise Conflict("You already have an application for this product.", code="application_exists",
                       extra={"applicationId": open_one.id})
    now = now_utc()
    app = FarmerLoanApplication(
        reference=_reference(db), user_id=user_id, lender_id=product.lender.id, product_id=product.id,
        product_version=product.version, product_snapshot=product.model_dump(mode="json", exclude={"lender"}),
        amount=amount, months=months, purpose=purpose.strip(), answers={}, status="draft",
        created_at=now, updated_at=now,
    )
    db.add(app)
    db.flush()
    _event(db, app, "draft", "farmer", "Farmer", "Application started.")
    db.commit()
    return out(db, app)


def list_for(db: DB, user_id: str) -> list[dict]:
    rows = db.scalars(select(FarmerLoanApplication).where(FarmerLoanApplication.user_id == user_id)
                      .order_by(FarmerLoanApplication.created_at.desc())).all()
    return [summary_out(db, r) for r in rows]


def get(db: DB, user_id: str, app_id: str) -> dict:
    return out(db, _own(db, user_id, app_id))


def update(db: DB, user_id: str, app_id: str, changes: dict) -> dict:
    app = _own(db, user_id, app_id, lock=True)
    if app.status not in EDITABLE:
        raise Conflict("This application can no longer be changed.", code="not_editable")
    if changes.get("amount") is not None:
        app.amount = changes["amount"]
    if changes.get("months") is not None:
        app.months = changes["months"]
    if changes.get("purpose") is not None:
        app.purpose = changes["purpose"].strip()
    if "letter" in changes and changes["letter"] is not None:
        letter = changes["letter"].strip()
        if len(letter) > LETTER_LIMIT:
            raise AppError(f"Keep your letter under {LETTER_LIMIT:,} characters.")
        app.letter = letter or None
    if changes.get("answers") is not None:
        reqs = {r["key"]: r for r in _requirements(app)}
        answers = dict(app.answers)
        for key, value in changes["answers"].items():
            req = reqs.get(key)
            if req is None or req.get("field_type") in ("none", "document"):
                raise AppError("That is not a question this product asks.")
            cleaned = _clean_answer(req, value)
            if cleaned is None:
                answers.pop(key, None)
            else:
                answers[key] = cleaned
        app.answers = answers
    app.updated_at = now_utc()
    db.commit()
    return out(db, app)


def _sniff_format(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def add_document(db: DB, user_id: str, app_id: str, requirement_key: str, data: bytes, filename: str) -> dict:
    app = _own(db, user_id, app_id, lock=True)
    if app.status not in EDITABLE:
        raise Conflict("This application can no longer be changed.", code="not_editable")
    req = next((r for r in _requirements(app) if r["key"] == requirement_key), None)
    if req is None or req.get("field_type") != "document":
        raise AppError("This product doesn't ask for a document there.")
    found = _sniff_format(data or b"")
    allowed = req.get("allowed_formats") or []
    if found is None or (allowed and found not in allowed):
        raise AppError(f"{req['name']}: upload {', '.join(f.upper() for f in allowed) or 'a PDF or image'}.")
    limit = (req.get("max_size_mb") or 5) * 1024 * 1024
    if len(data) > limit:
        raise AppError(f"{req['name']}: the file is larger than {req.get('max_size_mb') or 5} MB.")
    cleaned, content_type = clean_file(data)
    if sum(1 for d in _docs(db, app.id) if d.requirement_key == requirement_key) >= MAX_DOCUMENTS_PER_REQUIREMENT:
        raise AppError(f"{req['name']}: at most {MAX_DOCUMENTS_PER_REQUIREMENT} files.")
    doc = LoanApplicationDocument(application_id=app.id, requirement_key=requirement_key, file_key="",
                                  filename="", content_type=content_type, size_bytes=len(cleaned),
                                  sha256=hashlib.sha256(data).hexdigest(), uploaded_at=now_utc())
    doc.id = secrets.token_hex(16)
    doc.file_key = f"applications/{user_id}/{app.id}/{doc.id}"
    doc.filename = (re.sub(r"[\x00-\x1f\x7f]", "", (filename or "document").replace("\\", "/").split("/")[-1])[:255]
                    or "document")
    store(db).put(doc.file_key, cleaned, content_type)
    db.add(doc)
    app.updated_at = now_utc()
    db.commit()
    return out(db, app)


def remove_document(db: DB, user_id: str, app_id: str, document_id: str) -> dict:
    app = _own(db, user_id, app_id, lock=True)
    if app.status not in EDITABLE:
        raise Conflict("This application can no longer be changed.", code="not_editable")
    doc = db.get(LoanApplicationDocument, document_id)
    if doc is None or doc.application_id != app.id:
        raise NotFound("Document not found.")
    db.delete(doc)  # its stored file goes with it (trigger)
    app.updated_at = now_utc()
    db.commit()
    return out(db, app)


def document_file(db: DB, app: FarmerLoanApplication, document_id: str) -> tuple[bytes, str, str]:
    doc = db.get(LoanApplicationDocument, document_id)
    if doc is None or doc.application_id != app.id:
        raise NotFound("Document not found.")
    found = store(db).get(doc.file_key)
    if found is None:
        raise NotFound("Document not found.")
    return found[0], found[1], doc.filename


def own_document(db: DB, user_id: str, app_id: str, document_id: str) -> tuple[bytes, str, str]:
    return document_file(db, _own(db, user_id, app_id), document_id)


def _statement_months(period_months: int) -> int:
    return next((m for m in (3, 6, 12) if m >= period_months), 12)


def submit(db: DB, user_id: str, app_id: str, *, consent: bool, notice_version: str) -> dict:
    _loans_on(db)
    app = _own(db, user_id, app_id, lock=True)
    if app.status not in EDITABLE:
        raise Conflict("This application has already been submitted.", code="not_editable")
    if not consent or notice_version != CONSENT_VERSION:
        raise AppError("Read what will be shared and agree before submitting.", code="consent_required",
                       extra={"noticeVersion": CONSENT_VERSION, "dataItems": DATA_ITEMS})
    identity.assert_allowed(db, user_id, "loan_application")
    if lending.account_under_review(db, user_id):
        raise Forbidden(lending.ACCOUNT_REVIEW_MESSAGE, code="account_under_review")
    missing = missing_items(app, _docs(db, app.id))
    if missing:
        raise AppError("Finish your application first: " + "; ".join(missing), code="incomplete",
                       extra={"missing": missing})
    product = loan_products.listed_product(db, app.product_id) if app.product_id else None
    if product is None:
        raise Conflict("This loan product is no longer available.", code="product_unavailable")
    if product.version != app.product_version:
        # The lender changed its terms or requirements: the farmer reviews the new version.
        app.product_version = product.version
        app.product_snapshot = product.model_dump(mode="json", exclude={"lender"})
        app.updated_at = now_utc()
        db.commit()
        raise Conflict("The lender updated this product. Review its requirements, then submit again.",
                       code="product_updated")
    result = loan_eligibility.check(db, user_id, product.id, Decimal(app.amount), app.months)
    if result["outcome"] == "does_not_meet":
        raise AppError("A stated requirement of the lender is not met, so this can't be sent: "
                       + result["summary"], code="requirement_not_met", extra={"assessmentId": result["id"]})
    report = financial_reports.create(db, user_id, _statement_months(result["period"]["months"]))
    app = _own(db, user_id, app_id, lock=True)
    now = now_utc()
    lender = db.get(Lender, app.lender_id)
    db.add(LoanDataSharingConsent(
        application_id=app.id, user_id=user_id, lender_id=app.lender_id,
        purpose=f"To apply for {product.name} with {lender.name if lender else 'the lender'}",
        data_items=DATA_ITEMS, notice_version=notice_version, given_at=now,
    ))
    app.assessment_id, app.assessment_snapshot = result["id"], result
    app.statement_report_id = report["id"]
    app.submitted_at, app.review_note = now, None
    _set_status(db, app, "submitted", "farmer", "Farmer", "Submitted with consent to share it with the lender.")
    _notify(db, app, "Application submitted",
            f"AgriCtec is reviewing your application {app.reference} before sending it to the lender.")
    db.commit()
    return out(db, app)


def withdraw(db: DB, user_id: str, app_id: str, note: str | None) -> dict:
    app = _own(db, user_id, app_id, lock=True)
    if app.status in FINAL:
        raise Conflict("This application is already closed.", code="closed")
    was_sent = app.status in WITH_LENDER
    consent = _consent(db, app.id)
    if consent and consent.withdrawn_at is None:
        consent.withdrawn_at = now_utc()
    text = (note or "").strip()[:500] or None
    if was_sent:
        text = ((text + " ") if text else "") + "AgriCtec will tell the lender."
    _set_status(db, app, "withdrawn", "farmer", "Farmer", text)
    db.commit()
    return out(db, app)


# ---------- Admin ----------


def admin_list(db: DB, group: str | None = None) -> list[dict]:
    query = select(FarmerLoanApplication).where(FarmerLoanApplication.status != "draft")
    if group in GROUPS:
        query = query.where(FarmerLoanApplication.status.in_(GROUPS[group]))
    rows = db.scalars(query.order_by(FarmerLoanApplication.updated_at.desc()).limit(500)).all()
    from app.services.farmer_data import display_name

    return [{**summary_out(db, r), "farmer": display_name(db, r.user_id)} for r in rows]


def counts(db: DB) -> dict:
    rows = dict(db.execute(select(FarmerLoanApplication.status, func.count())
                           .group_by(FarmerLoanApplication.status)).all())
    return {
        "total": sum(v for k, v in rows.items() if k != "draft"),
        "drafts": rows.get("draft", 0),
        "needsAttention": sum(rows.get(s, 0) for s in ("submitted", "in_review", "approved_for_sending")),
        "withLender": sum(rows.get(s, 0) for s in WITH_LENDER),
    }


def tally(db: DB, app: FarmerLoanApplication) -> dict:
    """Do the figures sent still match the farmer's records? (The reviewer's check.)"""
    snap = app.assessment_snapshot or {}
    period = snap.get("period") or {}
    figures = snap.get("figures") or {}
    months = period.get("months") or 12
    farm_rows = db.scalars(select(FarmRecord).where(FarmRecord.user_id == app.user_id)).all()
    momo_rows = db.scalars(select(MomoTransaction).where(MomoTransaction.user_id == app.user_id)).all()
    a = analyse(entries_from(list(farm_rows), list(momo_rows), today()), months=months, today=today())
    now = {"income": f"{a.income:.2f}", "expenses": f"{a.expenses:.2f}", "net": f"{a.net:.2f}",
           "entries": a.entries, "verifiedEntries": a.verified_entries}
    lines = []
    for key, label in (("income", "Documented income"), ("expenses", "Documented expenses"), ("net", "Net cash flow"),
                       ("entries", "Entries"), ("verifiedEntries", "Verified entries")):
        then = figures.get(key)
        lines.append({"label": label, "atSubmission": then, "now": now[key], "same": str(then) == str(now[key])})
    report = db.get(FinancialReportExport, app.statement_report_id) if app.statement_report_id else None
    return {
        "lines": lines,
        "allMatch": all(line["same"] for line in lines),
        "statement": None if report is None else {
            "id": report.id, "periodMonths": report.period_months,
            "from": report.period_start.isoformat(), "to": report.period_end.isoformat(),
        },
        "note": "Figures are recalculated from the farmer's records now, over the same period. "
                "Differences mean entries were added, changed or reviewed since submission.",
    }


def admin_get(db: DB, actor: Admin, app_id: str) -> dict:
    app = _app(db, app_id)
    append_audit(db, actor, action="application.view", target_type="farmer", target_id=app.user_id,
                 summary=f"Viewed loan application {app.reference}")
    db.commit()
    profile = farm.get_profile(db, app.user_id)
    return {
        **out(db, app, for_admin=True),
        "farmer": {"name": profile.name, "phone": profile.phone, "farmName": profile.farm_name,
                   "region": profile.region, "constituency": profile.constituency,
                   "identityLevel": identity.identity_level(db, app.user_id)},
        "tally": tally(db, app),
    }


def admin_document(db: DB, actor: Admin, app_id: str, document_id: str) -> tuple[bytes, str, str]:
    app = _app(db, app_id)
    found = document_file(db, app, document_id)
    append_audit(db, actor, action="application.document", target_type="farmer", target_id=app.user_id,
                 summary=f"Opened a document of loan application {app.reference}")
    db.commit()
    return found


def admin_proof(db: DB, actor: Admin, app_id: str, event_id: int) -> tuple[bytes, str, str]:
    app = _app(db, app_id)
    event = db.get(LoanApplicationEvent, event_id)
    if event is None or event.application_id != app.id or not event.file_key:
        raise NotFound("No proof attached.")
    found = store(db).get(event.file_key)
    if found is None:
        raise NotFound("No proof attached.")
    return found[0], found[1], event.file_name or "proof"


def start_review(db: DB, actor: Admin, app_id: str) -> dict:
    app = _app(db, app_id, lock=True)
    if app.status != "submitted":
        raise Conflict("Only a submitted application can be taken into review.", code="bad_status")
    _set_status(db, app, "in_review", "admin", f"AgriCtec ({actor.name})", "Review started.")
    _audit(db, actor, app, "application.review", "Started reviewing")
    db.commit()
    return out(db, app, for_admin=True)


def request_changes(db: DB, actor: Admin, app_id: str, note: str) -> dict:
    app = _app(db, app_id, lock=True)
    if app.status not in REVIEWABLE:
        raise Conflict("Changes can only be requested while AgriCtec reviews it.", code="bad_status")
    note = (note or "").strip()
    if len(note) < 5:
        raise AppError("Tell the farmer what to change.")
    app.review_note = note
    _set_status(db, app, "changes_requested", "admin", f"AgriCtec ({actor.name})", note)
    _audit(db, actor, app, "application.changes_requested", "Asked the farmer for changes to")
    _notify(db, app, "Changes requested", f"AgriCtec asked for changes to application {app.reference}: {note}")
    db.commit()
    return out(db, app, for_admin=True)


def approve(db: DB, actor: Admin, app_id: str, note: str | None) -> dict:
    """AgriCtec's review passed: the application may be sent. Not a loan approval."""
    app = _app(db, app_id, lock=True)
    if app.status not in REVIEWABLE:
        raise Conflict("Only an application under AgriCtec review can be approved for sending.", code="bad_status")
    consent = _consent(db, app.id)
    if consent is None or consent.withdrawn_at is not None:
        raise Conflict("The farmer's consent to share is missing or withdrawn.", code="no_consent")
    _set_status(db, app, "approved_for_sending", "admin", f"AgriCtec ({actor.name})",
                (note or "").strip()[:1000] or "Checked against the farmer's records; ready to send.")
    _audit(db, actor, app, "application.approved_for_sending", "Approved for sending")
    db.commit()
    return send(db, actor, app.id)


def _attachments(db: DB, app: FarmerLoanApplication) -> list[tuple[str, str, bytes]]:
    files = [(f"{app.reference}-application.pdf", "application/pdf", pack_pdf(db, app))]
    report = db.get(FinancialReportExport, app.statement_report_id) if app.statement_report_id else None
    if report is not None:
        found = store(db).get(report.file_key)
        if found:
            files.append((f"{app.reference}-transaction-statement.pdf", "application/pdf", found[0]))
    for doc in _docs(db, app.id):
        found = store(db).get(doc.file_key)
        if found:
            files.append((doc.filename, found[1], found[0]))
    return files


def send(db: DB, actor: Admin, app_id: str) -> dict:
    """By email to the lender's agreed contact when there is one and email is set up; otherwise
    the application waits as "ready to send" for an administrator to deliver and record it."""
    app = _app(db, app_id, lock=True)
    if app.status != "approved_for_sending":
        raise Conflict("Only an application ready to send can be sent.", code="bad_status")
    lender = db.get(Lender, app.lender_id)
    if not (lender and lender.submission_email and lender.submission_agreed_on) or not get_settings().email_enabled:
        return {**out(db, app, for_admin=True), "sendResult": "manual"}
    body = (f"Loan application {app.reference} from {farm.get_profile(db, app.user_id).name}, prepared on AgriCtec "
            f"with the farmer's consent.\n\nProduct: {(app.product_snapshot or {}).get('name')}\n"
            f"Amount: GHS {app.amount:,.2f} over {app.months} months\nPurpose: {app.purpose}\n\n"
            "The application, the farmer's transaction statement (each entry marked verified or self-reported) "
            "and the documents the farmer uploaded are attached. AgriCtec's check is preliminary and not a loan "
            f"decision.\n\nPlease reply to confirm you received it, quoting {app.reference}.")
    try:
        messaging.send_email(lender.submission_email, f"AgriCtec loan application {app.reference}", body,
                             attachments=_attachments(db, app))
    except messaging.MessageFailed as error:
        return {**out(db, app, for_admin=True), "sendResult": "failed", "sendError": str(error)}
    _mark_sent(db, actor, app, via="email", to=lender.submission_email, note=f"Emailed to {lender.submission_email}.")
    db.commit()
    return {**out(db, app, for_admin=True), "sendResult": "sent"}


def record_sent(db: DB, actor: Admin, app_id: str, how: str) -> dict:
    app = _app(db, app_id, lock=True)
    if app.status != "approved_for_sending":
        raise Conflict("Only an application ready to send can be recorded as sent.", code="bad_status")
    how = (how or "").strip()
    if len(how) < 5:
        raise AppError("Say how and to whom it was delivered.")
    _mark_sent(db, actor, app, via="recorded", to=how[:200], note=how)
    db.commit()
    return out(db, app, for_admin=True)


def _mark_sent(db: DB, actor: Admin, app: FarmerLoanApplication, *, via: str, to: str, note: str) -> None:
    app.sent_at, app.sent_via, app.sent_to = now_utc(), via, to
    _set_status(db, app, "sent_to_lender", "admin", f"AgriCtec ({actor.name})", note)
    _audit(db, actor, app, "application.sent", f"Sent ({via})")
    lender = db.get(Lender, app.lender_id)
    _notify(db, app, "Sent to the lender",
            f"Your application {app.reference} was sent to {lender.name if lender else 'the lender'} for further "
            "review. The lender makes the decision; we'll tell you when it confirms any progress.")


def record_lender_status(db: DB, actor: Admin, app_id: str, status: str, note: str, proof: bytes,
                         proof_name: str) -> dict:
    """Only with the lender's own confirmation (its email or letter) attached."""
    app = _app(db, app_id, lock=True)
    if status not in LENDER_STATUSES:
        raise AppError("Choose a status the lender confirmed.")
    if app.status not in WITH_LENDER:
        raise Conflict("Lender updates are recorded only after the application was sent.", code="bad_status")
    note = (note or "").strip()
    if len(note) < 5:
        raise AppError("Say what the lender confirmed.")
    if not proof:
        raise AppError("Attach the lender's confirmation (its email or letter).", code="proof_required")
    cleaned, content_type = clean_file(proof)
    key = f"applications/{app.user_id}/{app.id}/proof-{secrets.token_hex(8)}"
    store(db).put(key, cleaned, content_type)
    name = re.sub(r"[\x00-\x1f\x7f]", "", (proof_name or "lender-confirmation").replace("\\", "/").split("/")[-1])[:255]
    _set_status(db, app, status, "admin", f"AgriCtec ({actor.name}), confirmed by the lender", note,
                file_key=key, file_name=name or "lender-confirmation")
    _audit(db, actor, app, "application.lender_status", f"Recorded the lender's confirmation ({status}) for")
    _notify(db, app, STATUS_LABELS[status], f"{STATUS_LABELS[status]} (confirmed by the lender): {note}")
    db.commit()
    return out(db, app, for_admin=True)


def _audit(db: DB, actor: Admin, app: FarmerLoanApplication, action: str, verb: str) -> None:
    append_audit(db, actor, action=action, target_type="farmer", target_id=app.user_id,
                 summary=f"{verb} loan application {app.reference}")


def pack_pdf(db: DB, app: FarmerLoanApplication) -> bytes:
    lender = db.get(Lender, app.lender_id)
    profile = farm.get_profile(db, app.user_id).model_dump()
    reqs = {r["key"]: r for r in _requirements(app)}
    answers = [{"name": reqs[k]["name"], "value": v} for k, v in app.answers.items() if k in reqs]
    documents = [{"requirementName": reqs.get(d.requirement_key, {}).get("name", d.requirement_key),
                  "filename": d.filename} for d in _docs(db, app.id)]
    consent = _consent(db, app.id)
    level = identity.identity_level(db, app.user_id)
    report = db.get(FinancialReportExport, app.statement_report_id) if app.statement_report_id else None
    data = {
        "reference": app.reference, "lenderName": lender.name if lender else "", "productSnapshot": app.product_snapshot,
        "productVersion": app.product_version, "amount": f"{app.amount:.2f}", "months": app.months,
        "purpose": app.purpose, "letter": app.letter, "answerRows": answers, "assessment": app.assessment_snapshot,
        "statementReference": report.id[:8].upper() if report else None,
        "identityLabel": {"none": "Not reviewed", "reviewed": "Reviewed by AgriCtec (not an NIA identity check)",
                          "nia_verified": "Verified with NIA"}[level],
    }
    consent_data = None if consent is None else {
        "givenAt": consent.given_at.astimezone(UTC).strftime("%d %b %Y %H:%M GMT"),
        "purpose": consent.purpose, "dataItems": consent.data_items,
    }
    return render_pack(application=data, profile=profile, documents=documents, consent=consent_data,
                       generated_at=datetime.now(UTC))


def own_pack(db: DB, user_id: str, app_id: str) -> bytes:
    return pack_pdf(db, _own(db, user_id, app_id))


def admin_pack(db: DB, actor: Admin, app_id: str) -> bytes:
    app = _app(db, app_id)
    _audit(db, actor, app, "application.pack", "Downloaded the application pack of")
    db.commit()
    return pack_pdf(db, app)


def product_for(db: DB, product_id: str) -> LoanProduct | None:
    return db.get(LoanProduct, product_id)
