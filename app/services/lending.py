"""Lenders and loan applications (services/admin/lenders.service.ts,
services/admin/loan-applications.service.ts, services/loans.service.ts)."""

import re
import secrets
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.events import bus
from app.domain.clock import now_utc
from app.domain.credit import calculate_credit_score
from app.domain.csv import to_csv
from app.domain.loans import (
    LOAN_STATUS_LABELS,
    check_eligibility,
    deactivate_block,
    delete_block,
    eligibility_checks,
    ghana_card_loan_message,
    monthly_repayment,
    status_change_error,
)
from app.models import Admin, Lender, LoanApplication, LoanStatusEvent, Profile, User
from app.schemas import farm as f
from app.schemas.admin import LoanReview, StatusChange
from app.schemas.platform import Lender as LenderOut
from app.schemas.platform import LenderInput, LenderPatch
from app.services import notifications, platform_store
from app.services.farmer_data import load_farmer, load_farmers
from app.services.mappers import lender_out, loan_out

# What the farmer is told at each step of the pipeline.
LOAN_NOTICES = {
    "submitted": ("application received", "The lender will review it soon."),
    "under_review": ("under review", "The lender is checking your records."),
    "approved": ("approved", "The lender approved your loan."),
    "rejected": ("not approved", "The lender did not approve this application."),
    "disbursed": ("funds sent", "The loan has been paid out to you."),
    "repaid": ("fully repaid", "Thank you. This loan is closed."),
    "defaulted": ("in default", "Contact the lender to agree a way forward."),
}

# ---------- Lenders ----------


def _lender(db: DB, lender_id: str) -> Lender:
    row = db.get(Lender, lender_id)
    if row is None:
        raise NotFound("Lender not found.")
    return row


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def _make_id(db: DB, name: str) -> str:
    """"Sinapi Aba Trust" -> "sinapi-aba-trust", with a number added if taken."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "lender"
    candidate, n = base, 2
    while db.get(Lender, candidate):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _all_applications(db: DB) -> list[f.LoanApplication]:
    return [loan_out(r) for r in db.scalars(select(LoanApplication))]


def create_lender(db: DB, actor: Admin, data: LenderInput) -> LenderOut:
    if any(_same_name(row.name, data.name) for row in db.scalars(select(Lender))):
        raise Conflict("A lender with this name already exists.")
    row = Lender(id=_make_id(db, data.name), created_at=now_utc(), **data.model_dump())
    db.add(row)
    db.flush()
    lender = lender_out(row)
    append_audit(db, actor, action="lender.create", target_type="lender", target_id=row.id,
                 summary=f"Added lender {row.name}", after=lender)
    db.commit()
    bus.publish("platform")
    return lender


def update_lender(db: DB, actor: Admin, lender_id: str, patch: LenderPatch) -> LenderOut:
    row = _lender(db, lender_id)
    before = lender_out(row)
    merged = {**before.model_dump(include=set(LenderInput.model_fields)), **patch.model_dump(exclude_unset=True)}
    data = LenderInput.model_validate(merged)
    if any(r.id != lender_id and _same_name(r.name, data.name) for r in db.scalars(select(Lender))):
        raise Conflict("A lender with this name already exists.")
    # Switching a lender off through an edit follows the same rule as the toggle.
    if before.active and not data.active:
        blocked = deactivate_block(before, _all_applications(db))
        if blocked:
            raise AppError(blocked)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    after = lender_out(row)
    append_audit(db, actor, action="lender.update", target_type="lender", target_id=lender_id,
                 summary=f"Edited lender {row.name}", before=before, after=after)
    db.commit()
    bus.publish("platform")
    return after


def toggle_lender(db: DB, actor: Admin, lender_id: str) -> LenderOut:
    row = _lender(db, lender_id)
    current = lender_out(row)
    # Hidden from farmers at once, so not while applications are under review.
    blocked = deactivate_block(current, _all_applications(db))
    if blocked:
        raise AppError(blocked)
    row.active = not row.active
    append_audit(
        db, actor, action="lender.activate" if row.active else "lender.deactivate", target_type="lender",
        target_id=lender_id, summary=f"{'Activated' if row.active else 'Deactivated'} lender {row.name}",
        before={"active": current.active}, after={"active": row.active},
    )
    db.commit()
    bus.publish("platform")
    return lender_out(row)


def delete_lender(db: DB, actor: Admin, lender_id: str) -> None:
    """Only a lender with no applications at all can be deleted: deactivate it instead."""
    row = _lender(db, lender_id)
    current = lender_out(row)
    blocked = delete_block(current, _all_applications(db))
    if blocked:
        raise AppError(blocked)
    db.delete(row)
    append_audit(db, actor, action="lender.delete", target_type="lender", target_id=lender_id,
                 summary=f"Removed lender {row.name}", before=current, after={"deleted": True})
    db.commit()
    bus.publish("platform")


# ---------- Applications: farmer side ----------


def list_for_farmer(db: DB, user_id: str) -> list[f.LoanApplication]:
    rows = db.scalars(
        select(LoanApplication).where(LoanApplication.user_id == user_id).order_by(LoanApplication.created_at.desc())
    )
    return [loan_out(r) for r in rows]


def _reference(db: DB) -> str:
    """"APP-2026-48213", unique among applications."""
    year = now_utc().year
    while True:
        reference = f"APP-{year}-{10000 + secrets.randbelow(90000)}"
        if not db.scalar(select(LoanApplication.id).where(LoanApplication.reference == reference)):
            return reference


def eligibility(db: DB, user_id: str, query: f.EligibilityQuery) -> f.EligibilityResult:
    data = load_farmer(db, user_id)
    config = platform_store.credit_config(db)
    score = calculate_credit_score(data.records, data.momo, data.profile, config).score
    partner = None
    if query.partner_id:
        partner = next((l for l in platform_store.lenders(db) if l.id == query.partner_id), None)
        if partner is None:
            raise NotFound("This lender is not accepting applications right now.")
    return check_eligibility(
        amount=query.amount,
        records=data.records,
        score=score,
        rules=config.eligibility,
        partner=partner,
        ghana_card=data.profile.ghana_card.status,
    )


def submit(db: DB, user_id: str, data: f.LoanApplicationInput) -> f.LoanApplication:
    """Starts as "submitted" and the admin moves it on. Every rule the loans page shows is
    checked again here: the rate, the monthly payment and the score come from the server."""
    if not platform_store.flags(db).loans:
        raise Forbidden("Loans are currently unavailable.", code="feature_off")
    farmer = load_farmer(db, user_id)
    if farmer.profile.ghana_card.status != "verified":
        raise Forbidden(ghana_card_loan_message(farmer.profile.ghana_card.status), code="ghana_card_required")
    lender = next((l for l in platform_store.lenders(db) if l.id == data.partner_id), None)
    if lender is None:
        raise AppError("This lender is not accepting applications right now.")
    if not lender.min_months <= data.months <= lender.max_months:
        raise AppError(f"{lender.name} lends for {lender.min_months}–{lender.max_months} months.")
    config = platform_store.credit_config(db)
    score = calculate_credit_score(farmer.records, farmer.momo, farmer.profile, config).score
    result = check_eligibility(
        amount=data.amount, records=farmer.records, score=score, rules=config.eligibility, partner=lender,
        ghana_card=farmer.profile.ghana_card.status,
    )
    if not result.eligible:
        raise AppError(" ".join(result.reasons), code="not_eligible")

    now = now_utc()
    row = LoanApplication(
        reference=_reference(db),
        user_id=user_id,
        partner_id=lender.id,
        amount=data.amount,
        months=data.months,
        purpose=data.purpose,
        rate=lender.rate,
        monthly=monthly_repayment(data.amount, lender.rate, data.months).monthly,
        score_at_application=score,
        status="submitted",
        created_at=now,
    )
    row.history.append(
        LoanStatusEvent(position=0, status="submitted", by=farmer.profile.name or "Farmer", at=now)
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("Please try again.") from None
    bus.publish("loans")
    return loan_out(row)


# ---------- Applications: admin side ----------


def _application(db: DB, app_id: str) -> LoanApplication:
    row = db.get(LoanApplication, app_id)
    if row is None:
        raise NotFound("Loan application not found.")
    return row


def list_applications(
    db: DB,
    *,
    status: str | None = None,
    partner_id: str | None = None,
    user_id: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    sent_from: date | None = None,
    sent_to: date | None = None,
    region: str | None = None,
) -> list[f.LoanApplication]:
    """Newest first. `status` may list several, comma-separated."""
    query = select(LoanApplication).order_by(LoanApplication.created_at.desc())
    if status:
        query = query.where(LoanApplication.status.in_(status.split(",")))
    if partner_id:
        query = query.where(LoanApplication.partner_id == partner_id)
    if user_id:
        query = query.where(LoanApplication.user_id == user_id)
    if min_amount:
        query = query.where(LoanApplication.amount >= min_amount)
    if max_amount:
        query = query.where(LoanApplication.amount <= max_amount)
    if region:
        query = query.join(Profile, Profile.user_id == LoanApplication.user_id).where(Profile.region == region)
    apps = [loan_out(r) for r in db.scalars(query)]
    return [
        a
        for a in apps
        if (not sent_from or a.created_at.date() >= sent_from) and (not sent_to or a.created_at.date() <= sent_to)
    ]


def get_application(db: DB, app_id: str) -> f.LoanApplication:
    return loan_out(_application(db, app_id))


def review(db: DB, app_id: str) -> LoanReview:
    """The application's eligibility re-run with today's rules and the farmer's current data."""
    app = _application(db, app_id)
    if db.get(User, app.user_id) is None:
        return LoanReview(farmer_exists=False, current_score=None, checks=[])
    farmer = load_farmer(db, app.user_id)
    config = platform_store.credit_config(db)
    score = calculate_credit_score(farmer.records, farmer.momo, farmer.profile, config).score
    lender = next((l for l in platform_store.lenders(db, include_inactive=True) if l.id == app.partner_id), None)
    checks = eligibility_checks(
        amount=float(app.amount), months=app.months, records=farmer.records, score=score, lender=lender,
        rules=config.eligibility,
    )
    return LoanReview.model_validate({"farmerExists": True, "currentScore": score, "checks": checks})


def update_status(db: DB, actor: Admin, app_id: str, change: StatusChange) -> f.LoanApplication:
    """Invalid jumps (e.g. submitted -> disbursed) are refused; approved, rejected and defaulted
    need a note; disbursed needs the date and the transfer reference."""
    row = _application(db, app_id)
    app = loan_out(row)
    now = now_utc()
    error = status_change_error(
        app,
        change.status,
        note=change.note,
        disbursed_on=change.disbursed_on,
        disbursement_ref=change.disbursement_ref,
        now=now,
        farmer_deleted=db.get(User, row.user_id) is None,
    )
    if error:
        raise AppError(error)
    note = (change.note or "").strip() or None
    disbursed = change.status == "disbursed"
    ref = (change.disbursement_ref or "").strip() if disbursed else None
    day = date.fromisoformat(change.disbursed_on) if disbursed and change.disbursed_on else None
    previous = row.status
    row.status = change.status
    row.history.append(
        LoanStatusEvent(
            position=len(row.history), status=change.status, by=actor.name, at=now, note=note,
            disbursed_on=day, disbursement_ref=ref,
        )
    )
    if disbursed:
        row.disbursement_date = day
        row.disbursement_ref = ref
    notice = None
    if db.get(User, row.user_id) is not None:
        title, body = LOAN_NOTICES[change.status]
        notice = notifications.create(
            db, row.user_id, "loan", f"{row.reference}: {title}", body + (f" Note: {note}" if note else ""), "/loans"
        )
    append_audit(
        db, actor, action="loan.status", target_type="loan", target_id=app_id,
        summary=f"{row.reference}: {LOAN_STATUS_LABELS[previous]} → {LOAN_STATUS_LABELS[change.status]}",
        before={"status": previous},
        after={"status": change.status, "note": note, "disbursedOn": day, "disbursementRef": ref},
    )
    db.commit()
    if notice:
        notifications.deliver(notice)
    bus.publish("account", row.user_id)
    return loan_out(row)


def export_csv(db: DB, actor: Admin, ids: list[str] | None) -> str:
    """No farmer contact details. Only the count is logged."""
    apps = [a for a in list_applications(db) if ids is None or a.id in ids]
    farmers = load_farmers(db, list({a.user_id for a in apps}))
    lenders = {l.id: l.name for l in platform_store.lenders(db, include_inactive=True)}

    def farmer(a, field: str) -> str:
        data = farmers.get(a.user_id)
        return getattr(data.profile, field) if data else ""

    csv = to_csv(
        apps,
        [
            ("Reference", lambda a: a.reference),
            ("Farmer", lambda a: farmer(a, "name")),
            ("Region", lambda a: farmer(a, "region")),
            ("Lender", lambda a: lenders.get(a.partner_id, "")),
            ("Amount (GHS)", lambda a: a.amount),
            ("Purpose", lambda a: a.purpose),
            ("Months", lambda a: a.months),
            ("Rate (%)", lambda a: a.rate),
            ("Monthly (GHS)", lambda a: a.monthly),
            ("Score at application", lambda a: a.score_at_application),
            ("Status", lambda a: LOAN_STATUS_LABELS[a.status]),
            ("Sent", lambda a: a.created_at.date().isoformat()),
            ("Disbursed on", lambda a: a.disbursement.date.isoformat() if a.disbursement else ""),
        ],
    )
    append_audit(
        db, actor, action="loan.export", target_type="loan",
        summary=f"Exported {len(apps)} loan application{'' if len(apps) == 1 else 's'} to CSV",
        after={"count": len(apps)},
    )
    db.commit()
    return csv
