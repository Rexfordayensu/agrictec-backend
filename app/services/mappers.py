"""Database rows to API shapes."""

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


def record_out(row: m.FarmRecord) -> f.FarmRecord:
    return f.FarmRecord(
        id=row.id,
        user_id=row.user_id,
        type=row.type,
        description=row.description,
        crop=row.crop,
        amount=float(row.amount),
        date=row.date,
        notes=row.notes,
        created_at=row.created_at,
    )


def momo_out(row: m.MomoTransaction) -> f.MomoTransaction:
    return f.MomoTransaction(
        id=row.id,
        user_id=row.user_id,
        direction=row.direction,
        counterparty=row.counterparty,
        amount=float(row.amount),
        reference=row.reference,
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
        image_thumb=row.image_thumb,
        date=row.date,
    )


def loan_out(row: m.LoanApplication) -> f.LoanApplication:
    return f.LoanApplication(
        id=row.id,
        reference=row.reference,
        user_id=row.user_id,
        partner_id=row.partner_id,
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


def lender_out(row: m.Lender) -> p.Lender:
    return p.Lender(
        id=row.id,
        name=row.name,
        rate=row.rate,
        max_amount=float(row.max_amount),
        min_months=row.min_months,
        max_months=row.max_months,
        min_score=row.min_score,
        color=row.color,
        active=row.active,
        description=row.description,
        created_at=row.created_at,
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
