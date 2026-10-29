"""Platform backup, restore and the danger zone (services/admin/backup.service.ts).

A backup holds every table except sign-ins, the team and the audit trail, which are never
exported or replaced. Password hashes never leave the server: restored accounts keep the
password they have now (an account that only exists in the backup can't sign in until an admin
resets its password). A restore is all or nothing: one transaction."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Date, DateTime, Numeric, delete, func, insert, select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError
from app.core.events import bus
from app.core.security import verify_password
from app.domain.clock import iso, now_utc, parse_iso
from app.domain.defaults import DEFAULT_CREDIT_CONFIG, DEFAULT_LENDERS
from app.models import (
    Admin,
    AdminNote,
    Announcement,
    ChatMessage,
    CreditConfigVersion,
    CreditReservation,
    Diagnosis,
    DismissedAnnouncement,
    FarmRecord,
    Lender,
    LoanApplication,
    LoanStatusEvent,
    MomoTransaction,
    Notification,
    OnboardingDraft,
    Payment,
    PlatformSetting,
    Profile,
    RiskFlag,
    Subscription,
    User,
    UserSettings,
    VerificationRequest,
)
from app.schemas.platform import CreditConfigInput, FeatureFlags, PlanList
from app.services import platform_store, seed

IMPORT_CONFIRMATION = "RESTORE"
RESET_CONFIRMATION = "RESET"
DELETE_FARMERS_CONFIRMATION = "DELETE ALL FARMERS"
BACKUP_VERSION = 2

# Parents before children (restored in this order, cleared in reverse).
TABLES = [
    User,
    Profile,
    FarmRecord,
    MomoTransaction,
    Diagnosis,
    ChatMessage,
    UserSettings,
    OnboardingDraft,
    AdminNote,
    Announcement,
    DismissedAnnouncement,
    Subscription,
    Payment,
    Notification,
    CreditReservation,
    CreditConfigVersion,
    PlatformSetting,
    Lender,
    LoanApplication,
    LoanStatusEvent,
    VerificationRequest,
    RiskFlag,
]
BY_NAME = {model.__tablename__: model for model in TABLES}
# Never in a file: the hash of a password, and the generated ID of a history row.
EXCLUDED_COLUMNS = {"users": {"password_hash"}, "loan_status_events": {"id"}}


def _columns(model) -> list:
    skip = EXCLUDED_COLUMNS.get(model.__tablename__, set())
    return [c for c in model.__table__.columns if c.name not in skip]


def _to_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _from_json(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return parse_iso(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    if isinstance(column.type, Numeric):
        return float(value)
    return value


def counts(db: DB) -> dict[str, int]:
    """What the platform holds: the before and after of a change that replaces or wipes it."""

    def count(query) -> int:
        return db.scalar(query) or 0

    return {
        "farmers": count(select(func.count()).select_from(User).where(User.is_guest.is_(False))),
        "applications": count(select(func.count()).select_from(LoanApplication)),
        "verifications": count(select(func.count()).select_from(VerificationRequest)),
        "riskFlags": count(select(func.count()).select_from(RiskFlag)),
    }


def export_all(db: DB, actor: Admin) -> dict:
    data = {}
    for model in TABLES:
        columns = _columns(model)
        rows = db.execute(select(*columns)).mappings()
        data[model.__tablename__] = [{c.name: _to_json(row[c.name]) for c in columns} for row in rows]
    total = sum(len(rows) for rows in data.values())
    append_audit(
        db, actor, action="system.export", target_type="system",
        summary=f"Exported a platform backup ({len(data)} tables, {total} rows)", after={"tables": len(data), "rows": total},
    )
    db.commit()
    return {"app": "agrictec", "kind": "platform-backup", "version": BACKUP_VERSION, "exportedAt": iso(now_utc()), "data": data}


def _parse(backup: dict) -> dict[str, list[dict]]:
    """Checks the file and every row before anything changes. Raises a readable error."""
    if backup.get("app") != "agrictec" or backup.get("kind") != "platform-backup":
        raise AppError("This is not an AgriCtec platform backup.")
    if backup.get("version") != BACKUP_VERSION:
        raise AppError(f"This backup is version {backup.get('version')}; this server restores version {BACKUP_VERSION}.")
    data = backup.get("data")
    if not isinstance(data, dict):
        raise AppError("The backup has no data.")
    unknown = sorted(set(data) - set(BY_NAME))
    if unknown:
        raise AppError(f"“{unknown[0]}” is not a table this server restores.")
    parsed: dict[str, list[dict]] = {}
    for name, rows in data.items():
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise AppError(f"“{name}” must be a list of rows.")
        columns = {c.name: c for c in _columns(BY_NAME[name])}
        out = []
        for i, row in enumerate(rows):
            extra = set(row) - set(columns)
            if extra:
                raise AppError(f"“{name}” row {i + 1} has an unknown field “{sorted(extra)[0]}”.")
            try:
                out.append({k: _from_json(columns[k], v) for k, v in row.items()})
            except (TypeError, ValueError) as error:
                raise AppError(f"“{name}” row {i + 1} is invalid: {error}") from error
        parsed[name] = out
    _check_documents(parsed)
    return parsed


def _check_documents(parsed: dict[str, list[dict]]) -> None:
    """The settings documents and credit rules get the same checks as a save in the console."""
    try:
        for row in parsed.get("platform_settings", []):
            if row["key"] == platform_store.FLAGS:
                FeatureFlags.model_validate(row["value"])
            elif row["key"] == platform_store.PLANS:
                PlanList.model_validate({"plans": row["value"]})
        for row in parsed.get("credit_config_versions", []):
            CreditConfigInput.model_validate(row)
    except (ValidationError, KeyError) as error:
        raise AppError(f"The backup's settings are invalid: {error}") from error


def summarize(backup: dict) -> dict:
    """What a backup holds, shown before restoring ("60 farmers, 25 applications...")."""
    parsed = _parse(backup)
    size = {name: len(rows) for name, rows in parsed.items()}
    return {
        "exportedAt": backup.get("exportedAt"),
        "tables": len(parsed),
        "farmers": sum(1 for u in parsed.get("users", []) if not u.get("is_guest")),
        "records": size.get("farm_records", 0),
        "momo": size.get("momo_transactions", 0),
        "applications": size.get("loan_applications", 0),
        "verifications": size.get("verification_requests", 0),
        "riskFlags": size.get("risk_flags", 0),
        "lenders": size.get("lenders", 0),
        "announcements": size.get("announcements", 0),
    }


def import_all(db: DB, actor: Admin, backup: dict, confirmation: str) -> dict:
    if confirmation != IMPORT_CONFIRMATION:
        raise AppError(f"Type {IMPORT_CONFIRMATION} to confirm the import.")
    parsed = _parse(backup)
    before = counts(db)
    passwords = dict(db.execute(select(User.id, User.password_hash)).all())
    for user in parsed.get("users", []):
        user["password_hash"] = passwords.get(user["id"])
    try:
        for model in reversed(TABLES):
            db.execute(delete(model))
        for model in TABLES:
            rows = parsed.get(model.__tablename__)
            if rows:
                db.execute(insert(model), rows)
        db.flush()
    except Exception as error:
        db.rollback()
        raise AppError(f"Import failed. The previous data has been restored. ({type(error).__name__})") from error
    after = {"rows": sum(len(r) for r in parsed.values()), **counts(db)}
    append_audit(db, actor, action="system.import", target_type="system", summary="Imported a platform backup",
                 before=before, after=after)
    db.commit()
    bus.publish("platform")
    return after


def delete_all_farmers(db: DB, actor: Admin, confirmation: str, password: str) -> dict:
    """Every farmer account and all farmer data, applications included. The team, the settings,
    the content and the audit log stay. Needs the phrase and the admin's own password."""
    if confirmation != DELETE_FARMERS_CONFIRMATION:
        raise AppError(f"Type {DELETE_FARMERS_CONFIRMATION} to confirm.")
    if not verify_password(password, actor.password_hash):
        raise AppError("That password is not correct.")
    before = counts(db)
    db.execute(delete(LoanApplication))
    db.execute(delete(User))
    append_audit(db, actor, action="system.delete_farmers", target_type="system",
                 summary=f"Deleted all farmer data ({before['farmers']} accounts)", before=before, after=counts(db))
    db.commit()
    bus.publish("platform")
    return {"farmers": before["farmers"]}


def reset_demo(db: DB, actor: Admin, confirmation: str) -> dict:
    """Rebuilds the seeded demo world and the default settings and content. Real accounts, the
    team and the audit trail are kept."""
    if confirmation != RESET_CONFIRMATION:
        raise AppError(f"Type {RESET_CONFIRMATION} to confirm the reset.")
    before = counts(db)
    seed.clear_seed_data(db)
    db.execute(delete(PlatformSetting))
    db.execute(delete(CreditConfigVersion))
    insert_default_credit_config(db)
    reset_default_lenders(db)
    seed.seed_demo(db, force=True)
    append_audit(db, actor, action="system.reset_demo", target_type="system", summary="Reset the demo data",
                 before=before, after=counts(db))
    db.commit()
    bus.publish("platform")
    return {"farmers": counts(db)["farmers"]}


# ---------- Defaults ----------


def insert_default_credit_config(db: DB) -> None:
    db.add(
        CreditConfigVersion(
            version=1,
            weights=DEFAULT_CREDIT_CONFIG["weights"],
            targets=DEFAULT_CREDIT_CONFIG["targets"],
            bands=DEFAULT_CREDIT_CONFIG["bands"],
            eligibility=DEFAULT_CREDIT_CONFIG["eligibility"],
            updated_at=parse_iso(DEFAULT_CREDIT_CONFIG["updatedAt"]),
            updated_by=DEFAULT_CREDIT_CONFIG["updatedBy"],
        )
    )


def reset_default_lenders(db: DB) -> None:
    """The four original lenders back to their original terms (others are left as they are)."""
    for lender in DEFAULT_LENDERS:
        row = db.get(Lender, lender["id"]) or Lender(id=lender["id"], created_at=parse_iso(lender["createdAt"]))
        row.name, row.rate, row.max_amount = lender["name"], lender["rate"], lender["maxAmount"]
        row.min_months, row.max_months, row.min_score = lender["minMonths"], lender["maxMonths"], lender["minScore"]
        row.color, row.active, row.description = lender["color"], True, None
        db.merge(row)


def bootstrap(db: DB, *, seed_demo_data: bool) -> None:
    """At startup: the first credit rules, the default lenders and the super admin; then the
    demo world when asked for and never seeded."""
    if not db.scalar(select(func.count()).select_from(CreditConfigVersion)):
        insert_default_credit_config(db)
    if not db.scalar(select(func.count()).select_from(Lender)):
        reset_default_lenders(db)
    seed.ensure_super_admin(db)
    db.commit()
    if seed_demo_data and seed.seed_demo(db):
        db.commit()
