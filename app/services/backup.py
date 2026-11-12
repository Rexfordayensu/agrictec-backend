"""Platform backup, restore and the danger zone (services/admin/backup.service.ts).

A backup holds every table except sign-ins, the team and the audit trail, which are never
exported or replaced. Password hashes never leave the server: restored accounts keep the
password they have now (an account that only exists in the backup can't sign in until an admin
resets its password). A restore is all or nothing: one transaction."""

import base64
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Date, DateTime, LargeBinary, Numeric, delete, func, insert, select, text
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core import backup_crypto
from app.core.audit import append_audit
from app.core.errors import AppError, Forbidden
from app.core.events import bus
from app.core.security import verify_password
from app.domain.clock import iso, now_utc, parse_iso
from app.domain.defaults import DEFAULT_CREDIT_CONFIG
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
    LoanProduct,
    LoanProductVersion,
    LoanRequirement,
    LoanApplication,
    LoanStatusEvent,
    LoanEligibilityAssessment,
    FarmerLoanApplication,
    LoanApplicationEvent,
    LoanApplicationDocument,
    LoanDataSharingConsent,
    MomoTransaction,
    Notification,
    OnboardingDraft,
    Payment,
    PlatformSetting,
    Profile,
    RiskFlag,
    Subscription,
    StoredFile,
    FinancialReportExport,
    IdentityVerification,
    TransactionEvidence,
    TransactionEvidenceLink,
    TransactionVerification,
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
    StoredFile,
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
    LoanProduct,
    LoanProductVersion,
    LoanRequirement,
    LoanApplication,
    LoanStatusEvent,
    LoanEligibilityAssessment,
    VerificationRequest,
    RiskFlag,
    FinancialReportExport,
    IdentityVerification,
    TransactionEvidence,
    TransactionEvidenceLink,
    TransactionVerification,
    FarmerLoanApplication,
    LoanApplicationEvent,
    LoanApplicationDocument,
    LoanDataSharingConsent,
]
BY_NAME = {model.__tablename__: model for model in TABLES}
# Never in a file: the hash of a password, and the generated ID of a history row.
EXCLUDED_COLUMNS = {"users": {"password_hash"}, "loan_status_events": {"id"}, "loan_application_events": {"id"}}


def _columns(model) -> list:
    skip = EXCLUDED_COLUMNS.get(model.__tablename__, set())
    return [c for c in model.__table__.columns if c.name not in skip]


def _to_json(value: Any) -> Any:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _from_json(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, LargeBinary):
        return base64.b64decode(value, validate=True)
    if isinstance(column.type, DateTime):
        return parse_iso(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    if isinstance(column.type, Numeric):
        return Decimal(str(value))
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


def export_all(db: DB, actor: Admin, passphrase: str) -> dict:
    """The whole platform, encrypted with the admin's passphrase (app/core/backup_crypto.py)."""
    problem = backup_crypto.passphrase_problem(passphrase)
    if problem:
        raise AppError(problem)
    data = {}
    for model in TABLES:
        columns = _columns(model)
        rows = db.execute(select(*columns)).mappings()
        data[model.__tablename__] = [{c.name: _to_json(row[c.name]) for c in columns} for row in rows]
    total = sum(len(rows) for rows in data.values())
    append_audit(
        db, actor, action="system.export", target_type="system",
        summary=f"Exported an encrypted platform backup ({len(data)} tables, {total} rows)",
        after={"tables": len(data), "rows": total},
    )
    exported_at = iso(now_utc())
    payload = {
        "app": "agrictec", "kind": "platform-backup", "version": BACKUP_VERSION, "exportedAt": exported_at, "data": data,
    }
    envelope = backup_crypto.encrypt(payload, passphrase, exported_at)
    db.commit()
    return envelope


def open_backup(document: dict, passphrase: str = "") -> dict:
    """The backup inside a file: decrypted with its passphrase, or, for a file made before backups
    were encrypted, as it is."""
    if backup_crypto.is_encrypted(document):
        return backup_crypto.decrypt(document, passphrase)
    return document


# Lenders in backups made before real lender management (migration 0007) carried made-up
# terms. Restoring one never brings those terms back, nor a sample lender, nor a listing.
LEGACY_LENDER_FIELDS = {"rate", "max_amount", "min_months", "max_months", "min_score", "active"}
SAMPLE_LENDERS = {
    "sinapi": "Sinapi Aba Trust",
    "opportunity": "Opportunity Fund",
    "agridev": "AgriDev Microfinance",
    "rural": "Rural Bank Consortium",
}


def _upgrade_lenders(rows: list, applications: Any) -> list:
    used = {
        a.get("partner_id") for a in applications if isinstance(a, dict)
    } if isinstance(applications, list) else set()
    out = []
    for row in rows:
        if not isinstance(row, dict) or not LEGACY_LENDER_FIELDS & set(row):
            out.append(row)
            continue
        sample = SAMPLE_LENDERS.get(row.get("id")) == row.get("name")
        if sample and row.get("id") not in used:
            continue
        upgraded = {k: v for k, v in row.items() if k not in LEGACY_LENDER_FIELDS}
        upgraded.update(
            institution_type=upgraded.get("institution_type") or "other",
            licence_status="unverified",
            listing_authorised=False,
            partnership_status="none",
            supported_regions=[],
            publication_status="suspended" if sample else "unpublished",
            suspension_reason="Sample lender from development: its terms were never real." if sample else None,
        )
        out.append(upgraded)
    return out


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
    if isinstance(data.get("lenders"), list):
        data = {**data, "lenders": _upgrade_lenders(data["lenders"], data.get("loan_applications"))}
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


def summarize(document: dict, passphrase: str = "") -> dict:
    """What a backup holds, shown before restoring ("60 farmers, 25 applications...")."""
    backup = open_backup(document, passphrase)
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


def import_all(db: DB, actor: Admin, document: dict, confirmation: str, password: str, passphrase: str = "") -> dict:
    """Replaces everything: needs the phrase and the admin's own password (like deleting every
    farmer), so an unattended or stolen session can't wipe the platform."""
    if confirmation != IMPORT_CONFIRMATION:
        raise AppError(f"Type {IMPORT_CONFIRMATION} to confirm the import.")
    if not verify_password(password, actor.password_hash):
        raise AppError("That password is not correct.")
    parsed = _parse(open_backup(document, passphrase))
    before = counts(db)
    passwords = dict(db.execute(select(User.id, User.password_hash)).all())
    for user in parsed.get("users", []):
        user["password_hash"] = passwords.get(user["id"])
    try:
        for model in reversed(TABLES):
            if model is not TransactionVerification:
                db.execute(delete(model))
        for model in TABLES:
            rows = parsed.get(model.__tablename__)
            if rows:
                if model is TransactionVerification:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    db.execute(pg_insert(model).values(rows).on_conflict_do_nothing(index_elements=["id"]))
                else:
                    db.execute(insert(model), rows)
        db.flush()
    except Exception as error:
        db.rollback()
        raise AppError(f"Import failed. The previous data has been restored. ({type(error).__name__})") from error
    # Immutable history is merged, never erased. If restoring an older snapshot changes the
    # current status, append that system action so the latest history agrees with the entry.
    for kind, model in (("farm", FarmRecord), ("momo", MomoTransaction)):
        for row in db.scalars(select(model)):
            last = db.scalar(select(TransactionVerification).where(
                TransactionVerification.record_kind == kind,
                TransactionVerification.record_id == row.id,
                TransactionVerification.user_id == row.user_id,
            ).order_by(TransactionVerification.at.desc(), TransactionVerification.id.desc()).limit(1))
            if last is None or last.to_status != row.verification_status:
                db.add(TransactionVerification(
                    record_kind=kind, record_id=row.id, user_id=row.user_id,
                    from_status=last.to_status if last else None, to_status=row.verification_status,
                    actor_type="system", actor_id="platform-restore", actor_name="AgriCtec",
                    reason="Restored the recorded status from a platform backup; no new verification inferred.", at=now_utc(),
                ))
    if not get_settings().demo_data_allowed:
        # A backup made in development can hold the demo world: it never reaches the live platform.
        seed.clear_seed_data(db)
        seed.disable_demo_team(db)
        db.execute(delete(PlatformSetting).where(PlatformSetting.key == platform_store.SEEDED))
        db.flush()
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


def reset_demo(db: DB, actor: Admin, confirmation: str, password: str) -> dict:
    """Rebuilds the seeded demo world and the default settings and content. Real accounts, the
    team and the audit trail are kept. Never in production (it would add made-up farmers to the
    live platform), and it needs the admin's own password."""
    if not get_settings().demo_data_allowed:
        raise Forbidden("The demo data can't be reset on the live platform.", code="production")
    if confirmation != RESET_CONFIRMATION:
        raise AppError(f"Type {RESET_CONFIRMATION} to confirm the reset.")
    if not verify_password(password, actor.password_hash):
        raise AppError("That password is not correct.")
    before = counts(db)
    seed.clear_seed_data(db)
    db.execute(delete(PlatformSetting))
    db.execute(delete(CreditConfigVersion))
    insert_default_credit_config(db)
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


# Any fixed number: the advisory lock that lets one API worker at a time run the bootstrap.
BOOTSTRAP_LOCK = 4_710_221


def _bootstrap_lock(db: DB) -> None:
    """Held until the transaction ends, so workers starting together don't insert twice."""
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK})


def bootstrap(db: DB, *, seed_demo_data: bool) -> None:
    """At startup: the first credit rules and the super admin; then the demo world when asked
    for and never seeded. No lender is ever created here: administrators add the real ones
    (Lenders page), and the marketplace stays empty until they publish one."""
    _bootstrap_lock(db)
    if not db.scalar(select(func.count()).select_from(CreditConfigVersion)):
        insert_default_credit_config(db)
    seed.ensure_super_admin(db)
    db.commit()
    if seed_demo_data:
        _bootstrap_lock(db)
        seed.seed_demo(db)
        db.commit()
