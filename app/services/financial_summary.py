"""The financial summary of one farmer (app/domain/financial_summary.py), from PostgreSQL."""

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import NotFound
from app.domain import financial_summary
from app.domain.clock import iso, today
from app.models import Admin, FarmRecord, MomoTransaction, User


def for_user(db: DB, user_id: str, months: int) -> dict:
    farm = list(db.scalars(select(FarmRecord).where(FarmRecord.user_id == user_id)))
    momo = list(db.scalars(select(MomoTransaction).where(MomoTransaction.user_id == user_id)))
    summary = financial_summary.build(farm, momo, months, today())
    summary["dataAsOf"] = iso(summary["dataAsOf"]) if summary["dataAsOf"] else None
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
