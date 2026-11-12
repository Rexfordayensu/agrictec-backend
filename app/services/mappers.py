"""Database rows to API shapes."""

from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import object_session

from app import models as m
from app.schemas import admin as a
from app.schemas import farm as f
from app.schemas import platform as p


def profile_out(row: m.Profile | None, user_id: str) -> f.Profile:
    if row is None:
        return f.Profile(user_id=user_id)
    status = row.ghana_card_status
    return f.Profile(
        user_id=row.user_id,
        name=row.name,
        photo_data_url=row.photo_data_url,
        email=row.email,
        phone=row.phone,
        farm_name=row.farm_name,
        use_name_as_farm_name=row.use_name_as_farm_name,
        region=row.region,
        constituency=row.constituency,
        community=row.community,
        gps=f.Gps(lat=row.gps_lat, lng=row.gps_lng) if row.gps_lat is not None and row.gps_lng is not None else None,
        acres=row.acres,
        size_unit=row.size_unit,
        primary_crops=list(row.primary_crops or []),
        other_crops=row.other_crops,
        farm_type=row.farm_type,
        cooperative_name=row.cooperative_name,
        outgrower_company=row.outgrower_company,
        ghana_card=f.GhanaCardInfo(
            status=status,
            masked=row.ghana_card_masked,
            hash=row.ghana_card_hash,
            submitted_at=row.ghana_card_submitted_at,
            rejected_reason=row.ghana_card_rejected_reason if status == "rejected" else None,
        ),
        verification=f.VerificationState(
            phone=row.verified_phone, location=row.verified_location, ghana_card=status == "verified"
        ),
        onboarding_completed_at=row.onboarding_completed_at,
    )


def transaction_metadata(row, kind: str, include_history: bool = True) -> dict:
    db = object_session(row) if include_history else None
    history = db.scalars(select(m.TransactionVerification).where(
        m.TransactionVerification.record_kind == kind, m.TransactionVerification.record_id == row.id,
        m.TransactionVerification.user_id == row.user_id,
    ).order_by(m.TransactionVerification.at, m.TransactionVerification.id)).all() if db else []
    count = db.scalar(select(func.count()).select_from(m.TransactionEvidenceLink).where(
        m.TransactionEvidenceLink.record_kind == kind, m.TransactionEvidenceLink.record_id == row.id,
    )) if db else 0
    return dict(category=row.category, currency=row.currency, source_type=row.source_type,
                verification_status=row.verification_status, payment_method=row.payment_method,
                external_reference=row.external_reference, updated_at=row.updated_at,
                evidence_count=count or 0, history=[f.StatusHistory.model_validate(h) for h in history])


def transaction_metadata_many(db, rows, kind: str) -> dict[str, dict]:
    """Load histories and evidence counts in two queries for an entire transaction list."""
    metadata = {row.id: transaction_metadata(row, kind, include_history=False) for row in rows}
    if not metadata:
        return metadata
    owners = {row.id: row.user_id for row in rows}
    history = db.scalars(select(m.TransactionVerification).where(
        m.TransactionVerification.record_kind == kind,
        m.TransactionVerification.record_id.in_(metadata),
        m.TransactionVerification.user_id.in_(set(owners.values())),
    ).order_by(m.TransactionVerification.at, m.TransactionVerification.id))
    for item in history:
        if item.user_id == owners[item.record_id]:
            metadata[item.record_id]["history"].append(f.StatusHistory.model_validate(item))
    counts = db.execute(select(
        m.TransactionEvidenceLink.record_id, func.count(),
    ).where(
        m.TransactionEvidenceLink.record_kind == kind,
        m.TransactionEvidenceLink.record_id.in_(metadata),
    ).group_by(m.TransactionEvidenceLink.record_id))
    for record_id, count in counts:
        metadata[record_id]["evidence_count"] = count
    return metadata


def record_out(row: m.FarmRecord, *, include_history: bool = True, metadata: dict | None = None) -> f.FarmRecord:
    return f.FarmRecord(
        id=row.id,
        user_id=row.user_id,
        type=row.type,
        description=row.description,
        crop=row.crop,
        amount=float(row.amount),
        date=row.date,
        notes=row.notes,
        direction=row.direction,
        **(metadata if metadata is not None else transaction_metadata(row, "farm", include_history)),
        created_at=row.created_at,
    )


def momo_out(row: m.MomoTransaction, *, include_history: bool = True, metadata: dict | None = None) -> f.MomoTransaction:
    return f.MomoTransaction(
        id=row.id,
        user_id=row.user_id,
        direction=row.direction,
        counterparty=row.counterparty,
        amount=float(row.amount),
        reference=row.reference,
        notes=row.notes,
        reference_duplicate=row.reference_duplicate,
        **(metadata if metadata is not None else transaction_metadata(row, "momo", include_history)),
        network=row.network,
        date=row.date,
        created_at=row.created_at,
    )


def diagnosis_out(row: m.Diagnosis) -> f.Diagnosis:
    return f.Diagnosis(
        id=row.id,
        user_id=row.user_id,
        disease=row.disease,
        severity=row.severity,
        confidence=row.confidence,
        treatment=row.treatment,
        prevention=row.prevention,
        crop=row.crop,
        # Only an inline JPEG (made by the API): never a link that would load from another site.
        image_thumb=row.image_thumb if (row.image_thumb or "").startswith("data:image/jpeg;base64,") else None,
        date=row.date,
        source=row.source,
    )


def loan_out(row: m.LoanApplication) -> f.LoanApplication:
    return f.LoanApplication(
        id=row.id,
        reference=row.reference,
        user_id=row.user_id,
        partner_id=row.partner_id,
        partner_name=row.lender.name if row.lender else None,
        amount=float(row.amount),
        months=row.months,
        purpose=row.purpose,
        rate=row.rate,
        monthly=float(row.monthly),
        score_at_application=row.score_at_application,
        status=row.status,
        history=[
            f.LoanHistoryEntry(
                status=h.status,
                by=h.by,
                at=h.at,
                note=h.note,
                disbursed_on=h.disbursed_on,
                disbursement_ref=h.disbursement_ref,
            )
            for h in row.history
        ],
        disbursement=f.Disbursement(date=row.disbursement_date, reference=row.disbursement_ref or "")
        if row.disbursement_date
        else None,
        created_at=row.created_at,
    )


def verification_out(row: m.VerificationRequest) -> f.VerificationRequest:
    return f.VerificationRequest.model_validate(row)


def risk_out(row: m.RiskFlag) -> a.RiskFlag:
    return a.RiskFlag.model_validate(row)


def announcement_out(row: m.Announcement) -> p.Announcement:
    return p.Announcement.model_validate(
        {
            "id": row.id,
            "title": row.title,
            "message": row.message,
            "type": row.type,
            "audience": row.audience,
            "startsAt": row.starts_at,
            "endsAt": row.ends_at,
            "active": row.active,
            "dismissible": row.dismissible,
            "createdBy": row.created_by,
            "createdAt": row.created_at,
        }
    )


def _logo_path(row: m.Lender, area: str) -> str | None:
    if not row.logo_key:
        return None
    version = int(row.logo_updated_at.timestamp()) if row.logo_updated_at else 0
    return f"/{area}/lenders/{row.id}/logo?v={version}"


def lender_logo_path(row: m.Lender) -> str | None:
    """The farmer-facing logo URL (only served while the lender is listed)."""
    return _logo_path(row, "platform")


def _lender_public_fields(row: m.Lender, area: str) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "institution_type": row.institution_type,
        "registered_name": row.registered_name,
        "description": row.description,
        "color": row.color,
        "logo_path": _logo_path(row, area),
        "website": row.website,
        "email": row.email,
        "phone": row.phone,
        "address": row.address,
        "region": row.region,
        "offers_agri_loans": row.offers_agri_loans,
        "supported_regions": list(row.supported_regions or []),
        "licence_ref": row.licence_ref,
        "regulator": row.regulator,
        "licence_status": row.licence_status,
        "verification_source": row.verification_source,
        "verified_at": row.verified_at,
        "partnership_status": row.partnership_status,
        "updated_at": row.updated_at or row.created_at,
    }


def lender_out(row: m.Lender) -> p.Lender:
    """The farmer-facing view (call it only for a lender that is listed)."""
    return p.Lender(**_lender_public_fields(row, "platform"))


def admin_lender_out(row: m.Lender, *, now: datetime, applications: int = 0) -> p.AdminLender:
    from app.domain import lenders as rules

    blockers = rules.listing_blockers(
        publication_status=row.publication_status,
        licence_status=row.licence_status,
        last_checked=row.verified_at,
        verification_source=row.verification_source,
        listing_authorised=row.listing_authorised,
        now=now,
    )
    return p.AdminLender(
        **_lender_public_fields(row, "admin"),
        publication_status=row.publication_status,
        listing_authorised=row.listing_authorised,
        listing_note=row.listing_note,
        verification_note=row.verification_note,
        verified_by=row.verified_by,
        verification_expires_at=rules.verification_expires(row.verified_at),
        review_due=rules.review_due(row.licence_status, row.verified_at, now),
        suspension_reason=row.suspension_reason,
        published_at=row.published_at,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        listed=not blockers,
        blockers=blockers,
        applications=applications,
        submission_email=row.submission_email,
        submission_agreed_on=row.submission_agreed_on,
        submission_note=row.submission_note,
    )


def admin_out(row: m.Admin) -> a.AdminUser:
    """Whitelists the public fields, so the password hash can never reach a response."""
    return a.AdminUser(
        id=row.id,
        name=row.name,
        email=row.email,
        role=row.role,
        status=row.status,
        must_change_password=row.must_change_password,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
        created_by=row.created_by,
    )


def audit_out(row: m.AuditEntry) -> a.AuditEntry:
    return a.AuditEntry.model_validate(row)


def credit_config_out(row: m.CreditConfigVersion) -> p.CreditConfig:
    return p.CreditConfig.model_validate(
        {
            "version": row.version,
            "weights": row.weights,
            "targets": row.targets,
            "bands": row.bands,
            "eligibility": row.eligibility,
            "updatedAt": row.updated_at,
            "updatedBy": row.updated_by,
            "note": row.note,
            "restoredFrom": row.restored_from,
        }
    )
