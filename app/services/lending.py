"""Lenders and loan applications (services/admin/lenders.service.ts,
services/admin/loan-applications.service.ts, services/loans.service.ts)."""

import hashlib
import io
import re
import secrets
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB
from PIL import Image

from app.core.audit import append_audit
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.events import bus
from app.domain.clock import now_utc
from app.domain.credit import calculate_credit_score
from app.domain.csv import to_csv
from app.domain.lenders import IDENTITY_FIELDS, listing_blockers, publish_blockers
from app.domain.loans import (
    LOAN_STATUS_LABELS,
    check_eligibility,
    delete_block,
    eligibility_checks,
    status_change_error,
)
from app.models import Admin, Lender, LoanApplication, LoanProduct, LoanStatusEvent, Profile, RiskFlag, User
from app.schemas import farm as f
from app.schemas.admin import LoanReview, StatusChange
from app.integrations import storage
from app.schemas.platform import AdminLender, LenderInput, LenderListing, LenderPatch, LenderSuspension, LenderVerification
from app.schemas.platform import Lender as LenderOut
from app.services import notifications, platform_store
from app.services.farmer_data import load_farmer, load_farmers
from app.services.mappers import admin_lender_out, lender_out, loan_out

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
#
# Administrators add real institutions from their official information, check the licence
# against the regulator's own source, record that the public listing was authorised, then
# publish. Farmers only ever see lenders that pass app/domain/lenders.py at the moment of
# reading. Every change is in the audit log and reaches open farmer tabs at once.

LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_MAX_SIDE = 512
LOGO_FORMATS = {"PNG", "JPEG", "WEBP"}


def _lender(db: DB, lender_id: str) -> Lender:
    row = db.get(Lender, lender_id)
    if row is None:
        raise NotFound("Lender not found.")
    return row


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def _make_id(db: DB, name: str) -> str:
    """"GCB Bank PLC" -> "gcb-bank-plc", with a number added if taken."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:70] or "lender"
    candidate, n = base, 2
    while db.get(Lender, candidate):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _application_counts(db: DB) -> dict[str, int]:
    rows = db.execute(select(LoanApplication.partner_id, func.count()).group_by(LoanApplication.partner_id))
    return {partner_id: count for partner_id, count in rows}


def _admin_view(db: DB, row: Lender, counts: dict[str, int] | None = None) -> AdminLender:
    counts = counts if counts is not None else _application_counts(db)
    return admin_lender_out(row, now=now_utc(), applications=counts.get(row.id, 0))


def _check_name(db: DB, name: str, own_id: str | None = None) -> None:
    if any(r.id != own_id and _same_name(r.name, name) for r in db.scalars(select(Lender))):
        raise Conflict("A lender with this name already exists.")


def _saved(db: DB, actor: Admin, row: Lender, *, action: str, summary: str, before, after) -> AdminLender:
    append_audit(db, actor, action=action, target_type="lender", target_id=row.id, summary=summary,
                 before=before, after=after)
    db.commit()
    bus.publish("platform")
    return _admin_view(db, row)


def _snapshot(row: Lender) -> dict:
    """The lender as the audit log records it."""
    return admin_lender_out(row, now=now_utc()).model_dump(mode="json", by_alias=True)


def list_admin_lenders(db: DB) -> list[AdminLender]:
    counts = _application_counts(db)
    rows = db.scalars(select(Lender).order_by(Lender.name))
    return [_admin_view(db, row, counts) for row in rows]


def get_admin_lender(db: DB, lender_id: str) -> AdminLender:
    return _admin_view(db, _lender(db, lender_id))


def create_lender(db: DB, actor: Admin, data: LenderInput) -> AdminLender:
    """A new lender starts as an unverified draft: farmers don't see it yet."""
    _check_name(db, data.name)
    now = now_utc()
    row = Lender(
        id=_make_id(db, data.name),
        **data.model_dump(),
        licence_status="unverified",
        publication_status="draft",
        listing_authorised=False,
        created_by=actor.name,
        created_at=now,
        updated_at=now,
        updated_by=actor.name,
    )
    db.add(row)
    db.flush()
    return _saved(db, actor, row, action="lender.create", summary=f"Added lender {row.name} (draft)",
                  before=None, after=_snapshot(row))


def update_lender(db: DB, actor: Admin, lender_id: str, patch: LenderPatch) -> AdminLender:
    """Changing who the institution is (name, registered name, licence, regulator or type)
    clears its verification: it leaves the marketplace until it is checked again."""
    row = _lender(db, lender_id)
    before = _snapshot(row)
    current = LenderInput.model_validate(
        {key: getattr(row, key) for key in LenderInput.model_fields}
    ).model_dump()
    merged = LenderInput.model_validate({**current, **patch.model_dump(exclude_unset=True)}).model_dump()
    _check_name(db, merged["name"], own_id=lender_id)
    changed = {key for key, value in merged.items() if current[key] != value}
    if not changed:
        return _admin_view(db, row)
    for key in changed:
        setattr(row, key, merged[key])
    identity_changed = changed & IDENTITY_FIELDS
    if identity_changed and row.licence_status != "unverified":
        row.licence_status = "unverified"
        row.verification_note = "Identity details changed: check the licence again."
        if row.publication_status == "published":
            row.publication_status = "unpublished"
    row.updated_at, row.updated_by = now_utc(), actor.name
    summary = f"Edited lender {row.name}"
    if identity_changed:
        summary += " (identity details changed: verification cleared)"
    return _saved(db, actor, row, action="lender.update", summary=summary, before=before, after=_snapshot(row))


def verify_lender(db: DB, actor: Admin, lender_id: str, check: LenderVerification) -> AdminLender:
    """Records the result of checking the licence against an official source. Anything but
    "verified" takes the lender off the marketplace."""
    row = _lender(db, lender_id)
    if check.checked_on > now_utc().date():
        raise AppError("The check date can't be in the future.")
    before = _snapshot(row)
    row.licence_status = check.licence_status
    row.verification_source = (check.source or "").strip() or None
    row.verification_note = (check.note or "").strip() or None
    checked = datetime.combine(check.checked_on, datetime.min.time(), tzinfo=UTC)
    row.verified_at = checked if check.licence_status != "unverified" else None
    row.verified_by = actor.name
    if check.licence_status != "verified" and row.publication_status == "published":
        row.publication_status = "unpublished"
    row.updated_at, row.updated_by = now_utc(), actor.name
    return _saved(db, actor, row, action="lender.verify",
                  summary=f"Recorded licence check for {row.name}: {check.licence_status}",
                  before=before, after=_snapshot(row))


def set_listing(db: DB, actor: Admin, lender_id: str, listing: LenderListing) -> AdminLender:
    row = _lender(db, lender_id)
    before = _snapshot(row)
    row.listing_authorised = listing.authorised
    row.listing_note = (listing.note or "").strip() or None
    if not listing.authorised and row.publication_status == "published":
        row.publication_status = "unpublished"
    row.updated_at, row.updated_by = now_utc(), actor.name
    verb = "Authorised" if listing.authorised else "Withdrew authorisation for"
    return _saved(db, actor, row, action="lender.listing", summary=f"{verb} the public listing of {row.name}",
                  before=before, after=_snapshot(row))


def publish_lender(db: DB, actor: Admin, lender_id: str) -> AdminLender:
    row = _lender(db, lender_id)
    if row.publication_status == "suspended":
        raise AppError("This lender is suspended. Reinstate it first.")
    blockers = publish_blockers(
        licence_status=row.licence_status, last_checked=row.verified_at,
        verification_source=row.verification_source, listing_authorised=row.listing_authorised,
        now=now_utc(),
    )
    if blockers:
        raise AppError(" ".join(blockers), code="not_publishable")
    before = _snapshot(row)
    now = now_utc()
    row.publication_status, row.published_at = "published", now
    row.updated_at, row.updated_by = now, actor.name
    return _saved(db, actor, row, action="lender.publish", summary=f"Published {row.name} to farmers",
                  before=before, after=_snapshot(row))


def unpublish_lender(db: DB, actor: Admin, lender_id: str) -> AdminLender:
    row = _lender(db, lender_id)
    if row.publication_status != "published":
        raise AppError("This lender is not published.")
    before = _snapshot(row)
    row.publication_status = "unpublished"
    row.updated_at, row.updated_by = now_utc(), actor.name
    return _saved(db, actor, row, action="lender.unpublish", summary=f"Unpublished {row.name}",
                  before=before, after=_snapshot(row))


def suspend_lender(db: DB, actor: Admin, lender_id: str, suspension: LenderSuspension) -> AdminLender:
    """Hidden from every farmer at once, whatever else is going on."""
    row = _lender(db, lender_id)
    before = _snapshot(row)
    row.publication_status = "suspended"
    row.suspension_reason = suspension.reason
    row.updated_at, row.updated_by = now_utc(), actor.name
    return _saved(db, actor, row, action="lender.suspend", summary=f"Suspended {row.name}: {suspension.reason}",
                  before=before, after=_snapshot(row))


def reinstate_lender(db: DB, actor: Admin, lender_id: str) -> AdminLender:
    """Back to unpublished: publishing again re-checks everything."""
    row = _lender(db, lender_id)
    if row.publication_status != "suspended":
        raise AppError("This lender is not suspended.")
    before = _snapshot(row)
    row.publication_status, row.suspension_reason = "unpublished", None
    row.updated_at, row.updated_by = now_utc(), actor.name
    return _saved(db, actor, row, action="lender.reinstate", summary=f"Lifted the suspension of {row.name}",
                  before=before, after=_snapshot(row))


def delete_lender(db: DB, actor: Admin, lender_id: str) -> None:
    """Only a lender with no applications and no published product (whose versions are its
    history): unpublish or suspend it instead. Unpublished draft products go with it."""
    row = _lender(db, lender_id)
    blocked = delete_block(row.name, _application_counts(db).get(lender_id, 0))
    if blocked:
        raise AppError(blocked)
    published = db.scalar(
        select(func.count()).where(
            LoanProduct.lender_id == lender_id, LoanProduct.published_version.is_not(None)
        )
    )
    if published:
        raise AppError(
            f"{row.name} has published loan products, and deleting it would erase their version "
            "history. Unpublish or suspend it instead."
        )
    before = _snapshot(row)
    logo = row.logo_key
    if logo:
        storage.store(db).delete(logo)
    db.delete(row)
    append_audit(db, actor, action="lender.delete", target_type="lender", target_id=lender_id,
                 summary=f"Removed lender {before['name']}", before=before, after={"deleted": True})
    db.commit()
    bus.publish("platform")


def _clean_logo(data: bytes) -> bytes:
    """The genuine logo, re-encoded: PNG, JPEG or WebP only (no SVG, which can carry scripts),
    metadata dropped, at most 512 px on its longest side."""
    if len(data) > LOGO_MAX_BYTES:
        raise AppError("The logo must be 2 MB or smaller.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        image = Image.open(io.BytesIO(data))
        if image.format not in LOGO_FORMATS:
            raise AppError("Upload the logo as a PNG, JPEG or WebP image.")
        image.load()
    except AppError:
        raise
    except Exception:  # noqa: BLE001 - anything Pillow can't read is not an image
        raise AppError("That file is not a readable image.") from None
    image.thumbnail((LOGO_MAX_SIDE, LOGO_MAX_SIDE))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    out = io.BytesIO()
    image.save(out, format="WEBP", quality=90, method=4)
    return out.getvalue()


def set_logo(db: DB, actor: Admin, lender_id: str, data: bytes) -> AdminLender:
    row = _lender(db, lender_id)
    cleaned = _clean_logo(data)
    digest = hashlib.sha256(cleaned).hexdigest()[:16]
    key = f"lenders/{row.id}/logo-{digest}.webp"
    files = storage.store(db)
    files.put(key, cleaned, "image/webp")
    old = row.logo_key
    row.logo_key, row.logo_updated_at = key, now_utc()
    row.updated_at, row.updated_by = row.logo_updated_at, actor.name
    if old and old != key:
        files.delete(old)
    return _saved(db, actor, row, action="lender.logo", summary=f"Uploaded the logo of {row.name}",
                  before={"logo": old}, after={"logo": key})


def remove_logo(db: DB, actor: Admin, lender_id: str) -> AdminLender:
    row = _lender(db, lender_id)
    old = row.logo_key
    if not old:
        return _admin_view(db, row)
    row.logo_key, row.logo_updated_at = None, now_utc()
    row.updated_at, row.updated_by = row.logo_updated_at, actor.name
    storage.store(db).delete(old)
    return _saved(db, actor, row, action="lender.logo", summary=f"Removed the logo of {row.name}",
                  before={"logo": old}, after={"logo": None})


def logo(db: DB, lender_id: str, *, listed_only: bool) -> tuple[bytes, str]:
    """The stored logo. Farmers only get the logos of listed lenders."""
    row = db.get(Lender, lender_id)
    if row is None or not row.logo_key or (listed_only and not is_listed(row)):
        raise NotFound("No logo.")
    found = storage.store(db).get(row.logo_key)
    if found is None:
        raise NotFound("No logo.")
    return found


# ---------- Lenders: what farmers see ----------


def is_listed(row: Lender) -> bool:
    return not listing_blockers(
        publication_status=row.publication_status, licence_status=row.licence_status,
        last_checked=row.verified_at, verification_source=row.verification_source,
        listing_authorised=row.listing_authorised, now=now_utc(),
    )


def listed_lenders(db: DB) -> list[LenderOut]:
    """The marketplace: the same catalogue for every farmer, checked at the moment of reading
    (a licence check that lapses overnight hides the lender without anyone acting)."""
    rows = db.scalars(
        select(Lender).where(Lender.publication_status == "published").order_by(Lender.name)
    )
    return [lender_out(row) for row in rows if is_listed(row)]


def listed_lender(db: DB, lender_id: str) -> LenderOut:
    row = db.get(Lender, lender_id)
    if row is None or not is_listed(row):
        raise NotFound("This lender is not available.")
    return lender_out(row)


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
    if query.partner_id:
        listed_lender(db, query.partner_id)  # 404 unless farmers can see it
    # The identity level loans need is a platform setting (reviewed card, or NIA-verified).
    from app.services import ghana_card_verification as identity

    needed = identity.requirements(db)["loan_application"]
    if needed == "none" or identity.meets(identity.identity_level(db, user_id), needed):
        return check_eligibility(ghana_card="verified")
    status = data.profile.ghana_card.status
    return check_eligibility(ghana_card=status if status != "verified" else "not_provided")


ACCOUNT_REVIEW_MESSAGE = (
    "Your account is being reviewed by AgriCtec. You can apply for a loan once the review is complete."
)


def account_under_review(db: DB, user_id: str) -> bool:
    """Whether an admin confirmed a risk flag on this account (no details leave the console)."""
    return bool(
        db.scalar(select(func.count()).where(RiskFlag.user_id == user_id, RiskFlag.status == "confirmed"))
    )


def submit(db: DB, user_id: str, data: f.LoanApplicationInput) -> f.LoanApplication:
    """Checks everything the farmer can fix first, then the lender. Applications need a loan
    product the lender published on AgriCtec, with its real terms: until a lender has one, nothing
    is sent (and no rate or payment is ever made up)."""
    if not platform_store.flags(db).loans:
        raise Forbidden("Loans are currently unavailable.", code="feature_off")
    from app.services import ghana_card_verification as identity

    identity.assert_allowed(db, user_id, "loan_application")
    # The loan report gives no recommendation while the account is under review; nor can the
    # farmer send one to a lender.
    if account_under_review(db, user_id):
        raise Forbidden(ACCOUNT_REVIEW_MESSAGE, code="account_under_review")
    lender = db.get(Lender, data.partner_id)
    if lender is None or not is_listed(lender):
        raise AppError("This lender is not available on AgriCtec right now.", code="lender_unavailable")
    from app.services.loan_products import has_listed_products  # (imports this module)

    if not has_listed_products(db, lender.id):
        raise AppError(
            f"{lender.name} has not published a loan product on AgriCtec yet, so applications can't be "
            "sent to it here. Contact the lender through its official channels.",
            code="no_loan_products",
        )
    # Applications against a product (with its requirements, documents and consent) open in a
    # later step; until then nothing is sent and no rate or payment is made up.
    raise AppError(
        f"Applying to {lender.name} through AgriCtec is not open yet. You can compare its loan "
        "products here and contact the lender through its official channels.",
        code="applications_not_open",
    )


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
    checks = eligibility_checks(
        records=farmer.records, ghana_card=farmer.profile.ghana_card.status, rules=config.eligibility
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
    lenders = {row.id: row.name for row in db.scalars(select(Lender))}

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
