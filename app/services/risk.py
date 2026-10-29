"""Risk and fraud review (services/admin/risk.service.ts)."""

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError, Forbidden, NotFound
from app.core.events import bus
from app.domain.clock import now_utc
from app.domain.permissions import can, permission_message
from app.domain.risk_rules import RISK_RULE_LABELS, RULES_BY_ID, SEVERITY_ORDER, RiskSubject, detect_risks, risk_key
from app.models import Admin, RiskFlag, User
from app.schemas import admin as a
from app.services import admin_farmers, platform_store
from app.services.farmer_data import build_account, confirmed_risk_counts, display_name, load_farmers
from app.services.mappers import risk_out


def run_scan(db: DB, actor: Admin) -> list[a.RiskFlag]:
    """Runs every rule over every account and stores only new findings. A finding is the same
    when rule + farmer + related IDs match, whatever its status, so a dismissed flag never returns."""
    farmers = load_farmers(db)
    subjects = [RiskSubject(uid, d.profile, d.records, d.momo) for uid, d in farmers.items()]
    known = set(db.scalars(select(RiskFlag.finding_key)))
    now = now_utc()
    found: list[RiskFlag] = []
    for risk in detect_risks(subjects):
        key = risk_key(risk.rule, risk.user_id, risk.related_ids)
        if key in known:
            continue
        known.add(key)
        row = RiskFlag(
            user_id=risk.user_id, rule=risk.rule, severity=risk.severity, description=risk.description,
            related_ids=risk.related_ids, finding_key=key, status="open", detected_at=now,
        )
        db.add(row)
        found.append(row)
    db.flush()
    append_audit(db, actor, action="risk.scan", target_type="risk", summary="Ran a risk scan",
                 after={"newFlags": len(found), "ids": [r.id for r in found]})
    db.commit()
    if found:
        bus.publish("risk")
    return [risk_out(r) for r in found]


def list_flags(
    db: DB, *, status: str | None = None, severity: str | None = None, rule: str | None = None, user_id: str | None = None
) -> list[a.RiskFlag]:
    """Open flags first, then by severity, then newest."""
    query = select(RiskFlag)
    for column, value in ((RiskFlag.status, status), (RiskFlag.severity, severity), (RiskFlag.rule, rule), (RiskFlag.user_id, user_id)):
        if value:
            query = query.where(column == value)
    flags = [risk_out(r) for r in db.scalars(query)]
    flags.sort(key=lambda f: f.detected_at, reverse=True)
    flags.sort(key=lambda f: (f.status != "open", SEVERITY_ORDER[f.severity]))
    return flags


def _review(db: DB, actor: Admin, flag_id: str, status: str, note: str) -> RiskFlag:
    row = db.get(RiskFlag, flag_id)
    if row is None:
        raise NotFound("Risk flag not found.")
    if row.status != "open":
        raise AppError("This flag has already been reviewed.")
    if len(note.strip()) < 3:
        raise AppError("Add a short note explaining your decision.")
    row.status = status
    row.reviewed_by = actor.name
    row.reviewed_at = now_utc()
    row.note = note.strip()
    verb = "Dismissed" if status == "dismissed" else "Confirmed"
    append_audit(
        db, actor, action=f"risk.{'dismiss' if status == 'dismissed' else 'confirm'}", target_type="risk",
        target_id=flag_id, summary=f"{verb} “{RISK_RULE_LABELS[row.rule]}” for {display_name(db, row.user_id)}",
        before={"status": "open"}, after={"status": status, "note": row.note},
    )
    return row


def dismiss(db: DB, actor: Admin, flag_id: str, note: str) -> a.RiskFlag:
    row = _review(db, actor, flag_id, "dismissed", note)
    db.commit()
    return risk_out(row)


def confirm(db: DB, actor: Admin, flag_id: str, note: str, suspend: bool) -> a.RiskFlag:
    """Confirms fraud. With `suspend`, the farmer is also suspended (its own audit entry); the
    suspend permission is checked first, so nothing changes if it is missing."""
    if suspend and not can(actor.role, "farmers.suspend"):
        raise Forbidden(permission_message("farmers.suspend"), code="forbidden")
    row = _review(db, actor, flag_id, "confirmed", note)
    db.commit()
    bus.publish("account", row.user_id)
    farmer = db.get(User, row.user_id)
    if suspend and farmer and farmer.status != "suspended":
        admin_farmers.suspend(db, actor, row.user_id, f"Confirmed fraud ({RISK_RULE_LABELS[row.rule]}): {note.strip()}")
    return risk_out(row)


def related(db: DB, flag_id: str) -> a.RiskRelated:
    """The flagged farmer, plus the related transactions, records or linked accounts."""
    row = db.get(RiskFlag, flag_id)
    if row is None:
        raise NotFound("Risk flag not found.")
    config = platform_store.credit_config(db)
    kind = RULES_BY_ID[row.rule].related
    ids = set(row.related_ids)
    wanted = [row.user_id, *row.related_ids] if kind == "accounts" else None
    farmers = load_farmers(db, wanted)
    risk = confirmed_risk_counts(db)
    if row.user_id not in farmers:
        raise NotFound("This farmer no longer exists.")

    def account(uid: str) -> a.FarmerAccount:
        return build_account(farmers[uid], config, risk.get(uid, 0))

    farmer = account(row.user_id)
    if kind == "accounts":
        others = [account(uid) for uid in row.related_ids if uid in farmers]
        return a.RiskRelated(kind=kind, farmer=farmer, accounts=[farmer, *others], momo=[], records=[])
    if kind == "momo":
        # A duplicate reference can sit on other accounts too: look through every farmer.
        momo = [
            a.RelatedMomo(**t.model_dump(), owner_name=d.profile.name or d.user.email, related=t.id in ids)
            for uid, d in farmers.items()
            for t in d.momo
            if t.id in ids or uid == row.user_id
        ]
        return a.RiskRelated(kind=kind, farmer=farmer, accounts=[], momo=momo, records=[])
    records = [a.RelatedRecord(**r.model_dump(), related=r.id in ids) for r in farmers[row.user_id].records]
    return a.RiskRelated(kind=kind, farmer=farmer, accounts=[], momo=[], records=records)
