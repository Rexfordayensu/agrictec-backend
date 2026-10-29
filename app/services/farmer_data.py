"""Reads farmers' data in bulk and builds the admin's view of them (lib/admin/farmer-data.ts)."""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from app.core.errors import NotFound
from app.domain.credit import calculate_credit_score
from app.domain.finance import total_expenses, total_income
from app.models import AdminNote, Diagnosis, FarmRecord, MomoTransaction, Profile, RiskFlag, User
from app.schemas import admin as a
from app.schemas import farm as f
from app.schemas.platform import CreditConfig
from app.services import mappers


@dataclass
class FarmerData:
    user: User
    profile: f.Profile
    records: list[f.FarmRecord] = field(default_factory=list)
    momo: list[f.MomoTransaction] = field(default_factory=list)
    diagnoses: list[f.Diagnosis] = field(default_factory=list)
    notes: list[a.AdminNote] = field(default_factory=list)


def _newest(item) -> tuple:
    return (item.date, item.created_at)


def load_farmers(db: DB, user_ids: list[str] | None = None) -> dict[str, FarmerData]:
    """Every farmer (or these), with their profile, records, MoMo, diagnoses and admin notes."""
    users_query = select(User).order_by(User.created_at, User.id)
    if user_ids is not None:
        users_query = users_query.where(User.id.in_(user_ids))
    users = db.scalars(users_query).all()
    ids = [u.id for u in users]
    if not ids:
        return {}

    profiles = {p.user_id: p for p in db.scalars(select(Profile).where(Profile.user_id.in_(ids)))}
    out = {u.id: FarmerData(user=u, profile=mappers.profile_out(profiles.get(u.id), u.id)) for u in users}
    for row in db.scalars(select(FarmRecord).where(FarmRecord.user_id.in_(ids))):
        out[row.user_id].records.append(mappers.record_out(row))
    for row in db.scalars(select(MomoTransaction).where(MomoTransaction.user_id.in_(ids))):
        out[row.user_id].momo.append(mappers.momo_out(row))
    for row in db.scalars(
        select(Diagnosis).where(Diagnosis.user_id.in_(ids)).order_by(Diagnosis.date.desc(), Diagnosis.created_at.desc())
    ):
        out[row.user_id].diagnoses.append(mappers.diagnosis_out(row))
    for row in db.scalars(select(AdminNote).where(AdminNote.user_id.in_(ids)).order_by(AdminNote.at.desc())):
        out[row.user_id].notes.append(a.AdminNote(id=row.id, text=row.text, by=row.by, at=row.at))
    for data in out.values():
        data.records.sort(key=_newest, reverse=True)
        data.momo.sort(key=_newest, reverse=True)
    return out


def load_farmer(db: DB, user_id: str) -> FarmerData:
    data = load_farmers(db, [user_id]).get(user_id)
    if data is None:
        raise NotFound("Farmer not found.")
    return data


def confirmed_risk_counts(db: DB, user_ids: list[str] | None = None) -> dict[str, int]:
    """Confirmed risk flags per farmer: they mark the account "under review"."""
    query = (
        select(RiskFlag.user_id, func.count())
        .where(RiskFlag.status == "confirmed")
        .group_by(RiskFlag.user_id)
    )
    if user_ids is not None:
        query = query.where(RiskFlag.user_id.in_(user_ids))
    return dict(db.execute(query).all())


def build_account(data: FarmerData, config: CreditConfig, risk_confirmed: int = 0) -> a.FarmerAccount:
    user = data.user
    times = [t for t in [user.last_active_at, *(r.created_at for r in data.records), *(t.created_at for t in data.momo)] if t]
    return a.FarmerAccount(
        user_id=user.id,
        email=user.email,
        is_guest=user.is_guest,
        status=user.status,
        suspended_reason=user.suspended_reason,
        created_at=user.created_at,
        last_active_at=max(times) if times else None,
        profile=data.profile,
        stats=a.FarmerStats(
            records=len(data.records),
            momo=len(data.momo),
            income=total_income(data.records),
            expenses=total_expenses(data.records),
            # The live credit rules, so admins see the same score the farmer sees.
            score=calculate_credit_score(data.records, data.momo, data.profile, config).score,
            diagnoses=len(data.diagnoses),
        ),
        risk_confirmed=risk_confirmed,
        tags=list(user.tags or []),
        admin_notes=data.notes,
    )


def accounts(db: DB, config: CreditConfig, user_ids: list[str] | None = None) -> list[a.FarmerAccount]:
    data = load_farmers(db, user_ids)
    risk = confirmed_risk_counts(db, user_ids)
    return [build_account(d, config, risk.get(uid, 0)) for uid, d in data.items()]


def display_name(db: DB, user_id: str, fallback: str = "a farmer") -> str:
    row = db.get(Profile, user_id)
    if row and row.name:
        return row.name
    user = db.get(User, user_id)
    return user.email if user else fallback
