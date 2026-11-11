"""Farmer administration (services/admin/farmers.service.ts, lib/admin/impersonation.ts)."""

import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError, Forbidden, NotFound
from app.core.events import bus
from app.core.security import hash_password, new_token, token_digest
from app.core.sessions import IMPERSONATION_HOURS, create_session, end_sessions_for
from app.domain.csv import to_csv
from app.domain.clock import now_utc
from app.domain.credit import calculate_credit_score, score_label
from app.domain.passwords import make_temp_password
from app.domain.verification import normalize_phone
from app.models import (
    Admin,
    AdminNote,
    Diagnosis,
    FarmRecord,
    ImpersonationToken,
    LoanApplication,
    MomoTransaction,
    User,
)
from app.schemas import admin as a
from app.services import notifications, platform_store, transactions
from app.services.farmer_data import accounts, build_account, confirmed_risk_counts, display_name, load_farmer

IMPERSONATION_TOKEN_SECONDS = 60


def _user(db: DB, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound("Farmer not found.")
    return user


def _account(db: DB, user_id: str) -> a.FarmerAccount:
    data = load_farmer(db, user_id)
    risk = confirmed_risk_counts(db, [user_id]).get(user_id, 0)
    return build_account(data, platform_store.credit_config(db), risk)


def list_farmers(
    db: DB,
    *,
    search: str | None = None,
    region: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    include_guests: bool = True,
) -> list[a.FarmerAccount]:
    """Every farmer account with their data summarised (filters are optional)."""
    items = accounts(db, platform_store.credit_config(db))
    term = (search or "").strip().lower()
    return [
        f
        for f in items
        if (include_guests or not f.is_guest)
        and (not region or f.profile.region == region)
        and (not status or f.status == status)
        and (not tag or tag in f.tags)
        and (
            not term
            or any(term in (v or "").lower() for v in (f.profile.name, f.email, f.profile.phone, f.profile.farm_name, f.user_id))
        )
    ]


def get_farmer(db: DB, user_id: str) -> a.FarmerAccount:
    _user(db, user_id)
    return _account(db, user_id)


def get_detail(db: DB, user_id: str) -> a.FarmerDetail:
    _user(db, user_id)
    data = load_farmer(db, user_id)
    config = platform_store.credit_config(db)
    risk = confirmed_risk_counts(db, [user_id]).get(user_id, 0)
    return a.FarmerDetail(
        account=build_account(data, config, risk),
        records=[],
        momo=[],
        transaction_status_counts=transactions.status_counts(db, user_id),
        diagnoses=data.diagnoses,
        credit=calculate_credit_score(data.records, data.momo, data.profile, config),
        credit_config=config,
    )


def suspend(db: DB, actor: Admin, user_id: str, reason: str) -> a.FarmerAccount:
    user = _user(db, user_id)
    reason = reason.strip()
    if len(reason) < 3:
        raise AppError("Give a reason for the suspension.")
    if user.status == "suspended":
        raise AppError("This farmer is already suspended.")
    before = {"status": user.status, "suspendedReason": user.suspended_reason}
    user.status = "suspended"
    user.suspended_reason = reason
    # Signed out everywhere at once.
    end_sessions_for(db, user_id=user_id)
    append_audit(
        db, actor, action="farmer.suspend", target_type="farmer", target_id=user_id,
        summary=f"Suspended {display_name(db, user_id, user.email)}: {reason}",
        before=before, after={"status": "suspended", "suspendedReason": reason},
    )
    notice = notifications.create(
        db, user_id, "account", "Your account is suspended", f"Reason: {reason} Contact support to appeal."
    )
    db.commit()
    notifications.deliver(notice)
    bus.publish("account", user_id)
    return _account(db, user_id)


def reactivate(db: DB, actor: Admin, user_id: str) -> a.FarmerAccount:
    user = _user(db, user_id)
    if user.status != "suspended":
        raise AppError("This farmer is not suspended.")
    before = {"status": user.status, "suspendedReason": user.suspended_reason}
    user.status = "active"
    user.suspended_reason = None
    append_audit(
        db, actor, action="farmer.reactivate", target_type="farmer", target_id=user_id,
        summary=f"Reactivated {display_name(db, user_id, user.email)}", before=before, after={"status": "active"},
    )
    notice = notifications.create(
        db, user_id, "account", "Your account is active again", "You can sign in and use AgriCtec as before.", "/dashboard"
    )
    db.commit()
    notifications.deliver(notice)
    bus.publish("account", user_id)
    return _account(db, user_id)


def delete_farmer(db: DB, actor: Admin, user_id: str, confirmation: str) -> None:
    """Permanently deletes the account and all its data (records, MoMo, profile, verifications,
    risk flags). Loan applications stay on record as "Farmer deleted": they hold no contact
    details, and lenders keep their history. `confirmation` is the farmer's email or name."""
    user = _user(db, user_id)
    name = display_name(db, user_id, user.email)
    typed = confirmation.strip().lower()
    profile_name = load_farmer(db, user_id).profile.name.strip().lower()
    if typed != user.email.lower() and (not profile_name or typed != profile_name):
        raise AppError("Type the farmer’s email or name exactly to confirm.")

    def count(model) -> int:
        return db.scalar(select(func.count()).where(model.user_id == user_id)) or 0

    # A summary only: counts, never the farmer's data or contact details.
    before = {
        "records": count(FarmRecord),
        "momo": count(MomoTransaction),
        "diagnoses": count(Diagnosis),
        "applications": count(LoanApplication),
    }
    db.delete(user)
    append_audit(
        db, actor, action="farmer.delete", target_type="farmer", target_id=user_id,
        summary=f"Permanently deleted {name}", before=before, after={"deleted": True},
    )
    db.commit()
    bus.publish("account", user_id)


def add_note(db: DB, actor: Admin, user_id: str, text: str) -> a.AdminNote:
    user = _user(db, user_id)
    if not text.strip():
        raise AppError("Write a note first.")
    if len(text) > 1000:
        raise AppError("Notes can be up to 1,000 characters.")
    note = AdminNote(user_id=user_id, text=text.strip(), by=actor.name, at=now_utc())
    db.add(note)
    db.flush()
    out = a.AdminNote(id=note.id, text=note.text, by=note.by, at=note.at)
    append_audit(
        db, actor, action="farmer.note", target_type="farmer", target_id=user_id,
        summary=f"Added a note to {display_name(db, user_id, user.email)}", after=out,
    )
    db.commit()
    return out


def normalize_tags(tags: list[str]) -> list[str]:
    """Lowercase, hyphenated, unique, at most 10: "Pilot 2026" -> "pilot-2026"."""
    clean = [re.sub(r"[^a-z0-9]+", "-", t.strip().lower()).strip("-") for t in tags]
    return list(dict.fromkeys(t for t in clean if t))[:10]


def set_tags(db: DB, actor: Admin, user_id: str, tags: list[str]) -> list[str]:
    user = _user(db, user_id)
    before = list(user.tags or [])
    user.tags = normalize_tags(tags)
    append_audit(
        db, actor, action="farmer.tags", target_type="farmer", target_id=user_id,
        summary=f"Set tags on {display_name(db, user_id, user.email)}: {', '.join(user.tags) or 'none'}",
        before=before, after=user.tags,
    )
    db.commit()
    return list(user.tags)


def reset_password(db: DB, actor: Admin, user_id: str) -> str:
    """A one-time password, returned once and never logged. The farmer must choose their own
    password when they sign in with it."""
    user = _user(db, user_id)
    if user.is_guest:
        raise AppError("The guest account has no password.")
    temporary = make_temp_password()
    user.password_hash = hash_password(temporary)
    user.must_change_password = True
    end_sessions_for(db, user_id=user_id)
    append_audit(
        db, actor, action="farmer.reset_password", target_type="farmer", target_id=user_id,
        summary=f"Reset the password for {display_name(db, user_id, user.email)}", after={"reset": True},
    )
    # Never the temporary password itself: the admin gives it to the farmer directly.
    notice = notifications.create(
        db, user_id, "account", "Your password was reset",
        "An AgriCtec admin reset your password. Sign in with the temporary password they gave you, then choose your own.",
    )
    db.commit()
    notifications.deliver(notice)
    return temporary


def export_records_csv(db: DB, actor: Admin, user_id: str) -> str:
    """Raw financial details are available only through the review queue."""
    raise Forbidden("Financial entries can only be viewed in the transaction review queue.")


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def export_csv(db: DB, actor: Admin, ids: list[str] | None) -> str:
    """A whitelist of columns: never a password hash, never Ghana Card digits (only the status)."""
    farmers = [f for f in list_farmers(db) if not f.is_guest and (ids is None or f.user_id in ids)]
    bands = platform_store.credit_config(db).bands
    csv = to_csv(
        farmers,
        [
            ("ID", lambda f: f.user_id),
            ("Name", lambda f: f.profile.name),
            ("Email", lambda f: f.email),
            ("Phone", lambda f: f.profile.phone),
            ("Region", lambda f: f.profile.region),
            ("Constituency", lambda f: f.profile.constituency),
            ("Community", lambda f: f.profile.community or ""),
            ("Farm", lambda f: f.profile.farm_name),
            ("Farm type", lambda f: f.profile.farm_type),
            ("Acres", lambda f: f.profile.acres),
            ("Primary crops", lambda f: "; ".join(f.profile.primary_crops)),
            ("GPS latitude", lambda f: f.profile.gps.lat if f.profile.gps else ""),
            ("GPS longitude", lambda f: f.profile.gps.lng if f.profile.gps else ""),
            ("Phone verified", lambda f: _yes_no(f.profile.verification.phone)),
            ("Location verified", lambda f: _yes_no(f.profile.verification.location)),
            ("Ghana Card verified", lambda f: _yes_no(f.profile.verification.ghana_card)),
            ("Ghana Card status", lambda f: f.profile.ghana_card.status),
            ("Status", lambda f: f.status),
            ("Suspension reason", lambda f: f.suspended_reason or ""),
            ("Credit score", lambda f: f.stats.score),
            ("Score band", lambda f: score_label(f.stats.score, bands)),
            ("Records", lambda f: f.stats.records),
            ("MoMo transactions", lambda f: f.stats.momo),
            ("Diagnoses", lambda f: f.stats.diagnoses),
            ("Income (GHS)", lambda f: f.stats.income),
            ("Expenses (GHS)", lambda f: f.stats.expenses),
            ("Net profit (GHS)", lambda f: f.stats.income - f.stats.expenses),
            ("Tags", lambda f: "; ".join(f.tags)),
            ("Joined", lambda f: f.created_at.isoformat()),
            ("Last active", lambda f: f.last_active_at.isoformat() if f.last_active_at else ""),
        ],
    )
    append_audit(
        db, actor, action="farmer.export", target_type="farmer",
        summary=f"Exported {len(farmers)} farmer{'' if len(farmers) == 1 else 's'} to CSV",
        after={"count": len(farmers)},
    )
    db.commit()
    return csv


# ---------- View as farmer (read-only) ----------


def start_impersonation(db: DB, actor: Admin, user_id: str) -> a.ImpersonationStart:
    """Audited, then a single-use token valid for one minute opens the farmer app read-only."""
    user = _user(db, user_id)
    if user.is_guest:
        raise AppError("The guest account can’t be viewed as a farmer.")
    token = new_token()
    now = now_utc()
    expires = now + timedelta(seconds=IMPERSONATION_TOKEN_SECONDS)
    db.add(ImpersonationToken(id=token_digest(token), admin_id=actor.id, user_id=user_id, created_at=now, expires_at=expires))
    append_audit(
        db, actor, action="farmer.impersonate", target_type="farmer", target_id=user_id,
        summary=f"Viewed the app as {display_name(db, user_id, user.email)} (read-only)", after={"readOnly": True},
    )
    db.commit()
    return a.ImpersonationStart(token=token, url=f"/dashboard?impersonate={token}", expires_at=expires)


def claim_impersonation(db: DB, admin: Admin, token: str) -> str:
    """Exchanges a token for a read-only session. Single use, within a minute, and only by the
    admin who started it. Returns the session token."""
    record = db.get(ImpersonationToken, token_digest(token))
    now = now_utc()
    if record is None or record.used_at or record.expires_at < now or record.admin_id != admin.id:
        raise AppError("This view-as-farmer link has expired. Start it again from the console.", code="invalid_token")
    record.used_at = now
    session_token, _ = create_session(
        db, "impersonation", admin_id=admin.id, user_id=record.user_id, ttl=timedelta(hours=IMPERSONATION_HOURS)
    )
    db.commit()
    return session_token


def end_impersonation(db: DB, admin: Admin, user_id: str) -> None:
    append_audit(
        db, admin, action="farmer.impersonate_end", target_type="farmer", target_id=user_id,
        summary=f"Stopped viewing the app as {display_name(db, user_id)}",
    )
    db.commit()


# ---------- The console's farmers list, one page at a time ----------

PAGE_SIZE_MAX = 100
VERIFICATIONS = ("full", "no-card", "no-phone")
_STATUS_ORDER = {"active": 0, "pending_verification": 1, "suspended": 2}


def _values(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _phone_forms(phone: str) -> str:
    """Both "+233241234567" and "0241234567", so a phone matches however it is typed."""
    if not phone:
        return ""
    intl = normalize_phone(phone)
    return f"{intl} {intl.replace('+233', '0', 1)}"


def _sort_value(f: a.FarmerAccount, key: str):
    v = f.profile.verification
    return {
        "farmer": (f.profile.name or f.email).lower(),
        "region": f.profile.region or "",
        "crops": ", ".join(f.profile.primary_crops),
        "score": f.stats.score,
        "records": f.stats.records,
        "income": f.stats.income,
        "verified": sum(1 for x in (v.phone, v.location, v.ghana_card) if x),
        "status": _STATUS_ORDER.get(f.status, 9),
        "joined": f.created_at,
    }.get(key, f.created_at)


def page_farmers(db: DB, params: dict[str, str | None], page: int, page_size: int) -> dict:
    """The farmers list's search, filters, sort and paging (lib/admin/farmer-list.ts), done here
    so the console only ever receives the page it shows. Guests are left out."""
    config = platform_store.credit_config(db)
    now = now_utc()
    people = [f for f in accounts(db, config) if not f.is_guest]
    summary = {
        "all": len(people),
        "active": sum(1 for f in people if f.status == "active"),
        "suspended": sum(1 for f in people if f.status == "suspended"),
        "pending_verification": sum(1 for f in people if f.status == "pending_verification"),
    }
    facets = {
        "regions": sorted({f.profile.region for f in people if f.profile.region}),
        "crops": sorted({c for f in people for c in f.profile.primary_crops}),
        "tags": sorted({t for f in people for t in f.tags}),
    }
    needle = (params.get("q") or "").strip().lower()
    regions, crops, bands = _values(params.get("region")), _values(params.get("crop")), _values(params.get("band"))
    status, tag = params.get("status"), params.get("tag")
    verification, records = params.get("verification"), params.get("records")
    joined_from, joined_to, active = params.get("joinedFrom"), params.get("joinedTo"), params.get("active")

    def keep(f: a.FarmerAccount) -> bool:
        v = f.profile.verification
        if needle and not any(
            needle in s.lower()
            for s in (f.profile.name, f.email, _phone_forms(f.profile.phone), f.profile.farm_name)
        ):
            return False
        if regions and f.profile.region not in regions:
            return False
        if crops and not any(c in f.profile.primary_crops for c in crops):
            return False
        if status and f.status != status:
            return False
        if bands and score_label(f.stats.score, config.bands).lower() not in bands:
            return False
        if verification == "full" and not (v.phone and v.location and v.ghana_card):
            return False
        if verification == "no-card" and v.ghana_card:
            return False
        if verification == "no-phone" and v.phone:
            return False
        joined = f.created_at.date().isoformat()
        if joined_from and joined < joined_from:
            return False
        if joined_to and joined > joined_to:
            return False
        if records == "yes" and f.stats.records == 0:
            return False
        if records == "no" and f.stats.records > 0:
            return False
        if tag and tag not in f.tags:
            return False
        if active:
            days = int(active) if active.isdigit() else 30
            if not f.last_active_at or (now.date() - f.last_active_at.date()).days > days:
                return False
        return True

    matching = [f for f in people if keep(f)]
    key = params.get("sort") or "joined"
    descending = (params.get("dir") or ("desc" if key == "joined" else "asc")) == "desc"
    matching.sort(key=lambda f: _sort_value(f, key), reverse=descending)
    size = max(1, min(PAGE_SIZE_MAX, page_size))
    pages = max(1, -(-len(matching) // size))
    current = max(1, min(page, pages))
    items = matching[(current - 1) * size : current * size]
    return {
        "items": [i.model_dump(mode="json", by_alias=True) for i in items],
        "total": len(matching),
        "page": current,
        "pageSize": size,
        "pageCount": pages,
        "summary": summary,
        "facets": facets,
        # Every matching ID, only when asked (for "export all filtered").
        **({"ids": [f.user_id for f in matching]} if params.get("ids") == "1" else {}),
    }


def lookup_farmers(db: DB, user_ids: list[str]) -> list[a.FarmerAccount]:
    """Just the farmers named: the ones behind the loans, verifications and risk flags the
    console shows, so it never needs everyone's details."""
    wanted = list(dict.fromkeys(i for i in user_ids if i))[:500]
    if not wanted:
        return []
    return accounts(db, platform_store.credit_config(db), wanted)
