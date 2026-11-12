"""The financial summary of one farmer (app/domain/financial_summary.py), from PostgreSQL."""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import NotFound
from app.domain import financial_summary
from app.domain.clock import iso, today
from app.models import Admin, FarmRecord, MomoTransaction, Profile, TransactionEvidenceLink, User
from app.services import mappers


def for_user(db: DB, user_id: str, months: int) -> dict:
    farm = list(db.scalars(select(FarmRecord).where(FarmRecord.user_id == user_id)))
    momo = list(db.scalars(select(MomoTransaction).where(MomoTransaction.user_id == user_id)))
    summary = financial_summary.build(farm, momo, months, today())
    summary["dataAsOf"] = iso(summary["dataAsOf"]) if summary["dataAsOf"] else None
    start, end = financial_summary.period_bounds(months, today())
    farm_now = [r for r in farm if start <= r.date <= end]
    momo_now = [r for r in momo if start <= r.date <= end]
    recent = sorted((r for r in farm_now if r.type in ("income", "expense")),
                    key=lambda r: (r.date, r.created_at, r.id), reverse=True)[:5]
    metadata = mappers.transaction_metadata_many(db, recent, "farm")
    summary["recentTransactions"] = [
        mappers.record_out(r, metadata=metadata[r.id]).model_dump(mode="json", by_alias=True)
        | {"amount": financial_summary.money(r.amount)} for r in recent
    ]
    links = db.execute(select(TransactionEvidenceLink.record_kind, TransactionEvidenceLink.record_id).where(or_(
        (TransactionEvidenceLink.record_kind == "farm") & TransactionEvidenceLink.record_id.in_([r.id for r in farm_now]),
        (TransactionEvidenceLink.record_kind == "momo") & TransactionEvidenceLink.record_id.in_([r.id for r in momo_now]),
    )).distinct()).all()
    profile = db.get(Profile, user_id)
    summary["recordReadiness"] = {
        "entriesWithEvidence": len(links),
        "identityVerified": bool(profile and profile.ghana_card_status == "verified"),
    }
    return summary


def for_admin(db: DB, actor: Admin, user_id: str, months: int) -> dict:
    """A farmer's figures for the console (farmers.view). Each look is in the audit log."""
    if db.get(User, user_id) is None:
        raise NotFound("Farmer not found.")
    summary = for_user(db, user_id, months)
    append_audit(db, actor, action="farmer.financial_summary", target_type="farmer", target_id=user_id,
                 summary=f"Viewed a farmer's {months}-month financial summary")
    db.commit()
    return summary
