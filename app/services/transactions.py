"""Private farmer transaction history, evidence and narrowly scoped review workflow."""

import hashlib
import io
import re
from collections import Counter
from decimal import Decimal

from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, func, literal, select, text, union_all
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.config import get_settings
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.events import bus
from app.domain.clock import add_months, iso, month_start, now_utc, today
from app.domain.transactions import MATERIAL_FIELDS, STATUSES, transition, validate_fields
from app.integrations.storage import store
from app.models import (
    FarmRecord,
    MomoTransaction,
    Profile,
    TransactionEvidence as Evidence,
    TransactionEvidenceLink as Link,
    TransactionVerification as History,
    new_id,
)
from app.services import mappers, notifications

MODELS = {"farm": FarmRecord, "momo": MomoTransaction}
MAX_BYTES = 5 * 1024 * 1024
MAX_FILES = 5
QUEUE = ("verification_pending", "disputed")


def entry(db: DB, kind: str, record_id: str, user_id: str | None = None, *, lock: bool = False):
    model = MODELS.get(kind)
    if model is None:
        raise NotFound("Transaction not found.")
    query = select(model).where(model.id == record_id)
    if user_id is not None:
        query = query.where(model.user_id == user_id)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise NotFound("Transaction not found.")
    return row


def evidence_rows(db: DB, kind: str, record_id: str):
    return db.scalars(
        select(Evidence)
        .join(Link, Link.evidence_id == Evidence.id)
        .where(
            Link.record_kind == kind,
            Link.record_id == record_id,
        )
        .order_by(Evidence.uploaded_at, Evidence.id)
    ).all()


def evidence_out(row):
    return dict(
        id=row.id,
        filename=row.filename,
        contentType=row.content_type,
        sizeBytes=row.size_bytes,
        kind=row.kind,
        uploadedAt=iso(row.uploaded_at),
    )


def item(db: DB, row, kind: str, *, include_evidence: bool = True):
    view = mappers.record_out(row) if kind == "farm" else mappers.momo_out(row)
    result = view.model_dump(mode="json", by_alias=True)
    result.update(kind=kind, amount=f"{row.amount:.2f}")
    if include_evidence:
        result["evidence"] = [evidence_out(e) for e in evidence_rows(db, kind, row.id)]
    return result


def change_status(db, row, kind, new_status, *, actor_type="farmer", actor=None, reason=None, initial=False):
    previous = None if initial else row.verification_status
    if previous == new_status:
        return
    db.add(
        History(
            id=new_id(),
            record_kind=kind,
            record_id=row.id,
            user_id=row.user_id,
            from_status=previous,
            to_status=new_status,
            actor_type=actor_type,
            actor_id=actor.id if actor else row.user_id,
            actor_name=actor.name if actor else "Farmer",
            reason=reason,
            at=now_utc(),
        )
    )
    row.verification_status, row.updated_at = new_status, now_utc()
    db.flush()


def prepare_write(db, user_id, kind, values, *, row=None, confirm_duplicate=False):
    # Serialises duplicate checks across API workers for this farmer.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"transactions:{user_id}"})
    current = {c.name: getattr(row, c.name) for c in row.__table__.columns} if row is not None else {}
    merged = {**current, **values}
    validate_fields(kind, merged)
    material = row is not None and any(k in MATERIAL_FIELDS and values[k] != current.get(k) for k in values)
    if row is not None and row.verification_status == "verified" and any(k != "notes" for k in values):
        raise Forbidden("Verified entries cannot be edited or deleted. Add a note or dispute the entry.")
    model = MODELS[kind]
    if kind == "momo" and merged.get("reference"):
        duplicate_ref = db.scalar(
            select(model.id)
            .where(
                model.user_id == user_id,
                func.upper(model.reference) == merged["reference"].upper(),
                model.id != row.id if row else literal(True),
            )
            .limit(1)
        )
        if duplicate_ref and (row is None or values.get("reference", row.reference) != row.reference):
            raise Conflict("This MoMo reference is already recorded for your account.", code="duplicate_reference")
    if (row is None or material) and not confirm_duplicate:
        query = select(model.id).where(
            model.user_id == user_id,
            model.date == merged["date"],
            model.amount == merged["amount"],
            model.category == merged.get("category"),
        )
        query = (
            query.where(model.type == merged["type"])
            if kind == "farm"
            else query.where(model.direction == merged["direction"])
        )
        if row is not None:
            query = query.where(model.id != row.id)
        if db.scalar(query.limit(1)):
            raise Conflict(
                "A possible duplicate has the same date, amount, type and category. Save anyway only if it is a separate entry.",
                code="possible_duplicate",
            )
    if material:
        new_status = transition(row.verification_status, "edit")
        # An edit is recorded even when the status was already self_reported.
        if row.verification_status == new_status:
            db.add(
                History(
                    record_kind=kind,
                    record_id=row.id,
                    user_id=user_id,
                    from_status=new_status,
                    to_status=new_status,
                    actor_type="farmer",
                    actor_id=user_id,
                    actor_name="Farmer",
                    reason="Financial details edited; evidence needs a fresh review.",
                    at=now_utc(),
                )
            )
        else:
            change_status(db, row, kind, new_status, reason="Financial details edited; evidence needs a fresh review.")
    return material


def delete_entry(db, kind, row):
    if row.verification_status == "verified":
        raise Forbidden("Verified entries cannot be deleted. Add a note or dispute the entry.")
    # The database trigger removes orphaned evidence and payloads in this transaction.
    db.delete(row)
    db.flush()


def list_history(
    db,
    user_id,
    *,
    kind=None,
    type=None,
    category=None,
    verification_status=None,
    source_type=None,
    date_from=None,
    date_to=None,
    page=1,
    page_size=25,
):
    queries = []
    for key, model in MODELS.items():
        if kind and kind != key:
            continue
        q = select(literal(key).label("kind"), model.id, model.date, model.created_at).where(model.user_id == user_id)
        if type:
            q = q.where((model.type if key == "farm" else model.direction) == type)
        if category:
            q = q.where(model.category.is_(None) if category == "uncategorised" else model.category == category)
        for name, value in (("verification_status", verification_status), ("source_type", source_type)):
            if value:
                q = q.where(getattr(model, name) == value)
        if date_from:
            q = q.where(model.date >= date_from)
        if date_to:
            q = q.where(model.date <= date_to)
        queries.append(q)
    combined = union_all(*queries).subquery()
    total = db.scalar(select(func.count()).select_from(combined)) or 0
    rows = db.execute(
        select(combined)
        .order_by(combined.c.date.desc(), combined.c.created_at.desc(), combined.c.kind, combined.c.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return dict(
        items=[item(db, entry(db, r.kind, r.id, user_id), r.kind) for r in rows],
        total=total,
        page=page,
        pageSize=page_size,
        pageCount=max(1, (total + page_size - 1) // page_size),
    )


def summary(db, user_id, months):
    end = today()
    start = add_months(month_start(end), -(months - 1))
    totals = {
        key: Decimal("0") for key in ("income", "expenses", "net", "momoIn", "momoOut", "financingIn", "financingOut")
    }
    counts = Counter({s: 0 for s in STATUSES})
    amount_total, verified_amount = Decimal("0"), Decimal("0")
    for kind, model in MODELS.items():
        rows = db.scalars(select(model).where(model.user_id == user_id, model.date >= start, model.date <= end))
        for row in rows:
            counts[row.verification_status] += 1
            amount_total += row.amount
            if row.verification_status == "verified":
                verified_amount += row.amount
            key = (
                ("momoIn" if row.direction == "in" else "momoOut")
                if kind == "momo"
                else (
                    "income"
                    if row.type == "income"
                    else "expenses"
                    if row.type == "expense"
                    else ("financingIn" if row.direction == "in" else "financingOut")
                    if row.type == "financing"
                    else None
                )
            )
            if key:
                totals[key] += row.amount
    totals["net"] = totals["income"] - totals["expenses"]
    total_count = sum(counts.values())
    return {
        **{k: f"{v:.2f}" for k, v in totals.items()},
        "currency": "GHS",
        "countsByStatus": dict(counts),
        "totalCount": total_count,
        "verifiedShareByCount": f"{Decimal(counts['verified']) / total_count * 100:.2f}" if total_count else "0.00",
        "verifiedShareByAmount": f"{verified_amount / amount_total * 100:.2f}" if amount_total else "0.00",
        "period": {"from": start.isoformat(), "to": end.isoformat(), "months": months},
    }


def clean_file(data: bytes):
    if not data or len(data) > MAX_BYTES:
        raise AppError("Choose a non-empty file no larger than 5 MB.")
    if data.startswith(b"%PDF-"):
        return data, "application/pdf"
    headers = (
        data.startswith(b"\xff\xd8\xff"),
        data.startswith(b"\x89PNG\r\n\x1a\n"),
        data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    )
    if not any(headers):
        raise AppError("Only PDF, JPEG, PNG and WebP evidence is accepted.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            out = io.BytesIO()
            image.convert("RGBA" if image.mode == "RGBA" else "RGB").save(out, format="WEBP", quality=90)
            cleaned = out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise AppError("This is not a readable evidence image.") from None
    if len(cleaned) > MAX_BYTES:
        raise AppError("The processed image exceeds 5 MB. Choose a smaller image.")
    return cleaned, "image/webp"


def link_file(db, user_id, kind, row, data, filename, evidence_kind, cleaned=None, content_type=None) -> bool:
    """Stores the file (once per farmer) and links it to the entry. False if it was already linked.
    The caller holds the farmer's evidence lock and commits."""
    if cleaned is None:
        cleaned, content_type = clean_file(data)
    digest = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(Evidence).where(Evidence.user_id == user_id, Evidence.sha256 == digest))
    if existing and db.get(Link, (existing.id, kind, row.id)):
        return False
    # A receipt proves one payment: the same receipt can't back a second entry. (A statement
    # or other document may cover several entries.)
    if existing is not None and "receipt" in (evidence_kind, existing.kind):
        used = db.scalar(select(func.count()).select_from(Link).where(Link.evidence_id == existing.id))
        if used:
            raise AppError(
                "This receipt is already attached to another transaction. Each receipt proves one payment.",
                code="receipt_reused",
            )
    if len(evidence_rows(db, kind, row.id)) >= MAX_FILES:
        raise AppError("Each transaction can have at most 5 evidence files.")
    if existing is None:
        quota = get_settings().evidence_quota_bytes
        stored = db.scalar(select(func.coalesce(func.sum(Evidence.size_bytes), 0)).where(Evidence.user_id == user_id))
        if stored + len(cleaned) > quota:
            raise AppError(
                f"Your receipts and evidence files have reached your {quota // (1024 * 1024)} MB storage "
                "limit. Remove files you no longer need, or contact AgriCtec.",
                code="storage_full",
            )
        evidence_id = new_id()
        key = f"evidence/{user_id}/{evidence_id}"
        store(db).put(key, cleaned, content_type)
        name = (
            re.sub(r"[\x00-\x1f\x7f]", "", (filename or "evidence").replace("\\", "/").split("/")[-1])[:255]
            or "evidence"
        )
        existing = Evidence(
            id=evidence_id,
            user_id=user_id,
            file_key=key,
            filename=name,
            content_type=content_type,
            size_bytes=len(cleaned),
            sha256=digest,
            kind=evidence_kind,
            uploaded_at=now_utc(),
        )
        db.add(existing)
        db.flush()
    db.add(Link(evidence_id=existing.id, record_kind=kind, record_id=row.id))
    return True


def lock_evidence(db, user_id):
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"evidence:{user_id}"})


def send_momo_for_review(db, row, reason, kind="momo"):
    """A MoMo entry (or a farm record paid by mobile money) with its receipt goes straight to
    AgriCtec's review queue."""
    next_status = transition(row.verification_status, "request_review", evidence_count=1)
    change_status(db, row, kind, next_status, reason=reason)


def paid_by_momo(kind, row) -> bool:
    return kind == "momo" or getattr(row, "payment_method", None) == "mobile_money"


def attach(db, user_id, kind, record_id, data, filename, evidence_kind):
    lock_evidence(db, user_id)
    row = entry(db, kind, record_id, user_id, lock=True)
    if not link_file(db, user_id, kind, row, data, filename, evidence_kind):
        return item(db, row, kind)
    change_status(
        db, row, kind, transition(row.verification_status, "attach"), reason="Evidence attached; not a verification."
    )
    queued = paid_by_momo(kind, row) and row.verification_status == "evidence_submitted"
    if queued:
        send_momo_for_review(db, row, "MoMo receipt uploaded; sent for review.", kind)
    db.commit()
    bus.publish("account", user_id)
    if queued:  # the admin review queue changed
        bus.publish("platform")
    return item(db, row, kind)


def remove_evidence(db, user_id, evidence_id):
    evidence = db.scalar(
        select(Evidence).where(Evidence.id == evidence_id, Evidence.user_id == user_id).with_for_update()
    )
    if evidence is None:
        raise NotFound("Evidence not found.")
    links = db.scalars(select(Link).where(Link.evidence_id == evidence.id)).all()
    for link in links:
        row = entry(db, link.record_kind, link.record_id, user_id, lock=True)
        next_status = transition(
            row.verification_status,
            "remove_evidence",
            evidence_count=len(evidence_rows(db, link.record_kind, row.id)) - 1,
        )
        change_status(db, row, link.record_kind, next_status, reason="Evidence removed.")
    db.execute(delete(Link).where(Link.evidence_id == evidence.id))
    db.delete(evidence)
    db.commit()
    bus.publish("account", user_id)


def farmer_action(db, user_id, kind, record_id, action, reason=None):
    row = entry(db, kind, record_id, user_id, lock=True)
    next_status = transition(
        row.verification_status, action, evidence_count=len(evidence_rows(db, kind, row.id)), reason=reason
    )
    change_status(db, row, kind, next_status, reason=reason)
    db.commit()
    bus.publish("account", user_id)
    bus.publish("platform")
    return item(db, row, kind)


def review_queue(db, actor, page, page_size):
    queries = [
        select(literal(k).label("kind"), m.id, m.updated_at).where(m.verification_status.in_(QUEUE))
        for k, m in MODELS.items()
    ]
    combined = union_all(*queries).subquery()
    total = db.scalar(select(func.count()).select_from(combined)) or 0
    selected = db.execute(
        select(combined)
        .order_by(combined.c.updated_at, combined.c.kind, combined.c.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = []
    for r in selected:
        row = entry(db, r.kind, r.id)
        profile = db.get(Profile, row.user_id)
        items.append(
            {
                "farmerName": profile.name if profile and profile.name else "Not provided",
                "region": profile.region if profile and profile.region else "Not provided",
                "entry": item(db, row, r.kind),
                "warnings": cross_account_warnings(db, r.kind, row),
            }
        )
    append_audit(
        db,
        actor,
        action="transaction.review_view",
        target_type="transaction",
        summary=f"Viewed transaction review queue ({len(items)} entries)",
        after={"entries": [{"kind": r.kind, "id": r.id} for r in selected]},
    )
    db.commit()
    return dict(
        items=items, total=total, page=page, pageSize=page_size, pageCount=max(1, (total + page_size - 1) // page_size)
    )


def cross_account_warnings(db, kind, row) -> list[str]:
    """What the reviewer should know before verifying: the same receipt file, or the same MoMo
    transaction ID, on another farmer's account. (A buyer and a seller can both record one payment,
    so this is a warning to check, not a block.)"""
    warnings = []
    digests = [e.sha256 for e in evidence_rows(db, kind, row.id)]
    if digests:
        others = db.scalar(
            select(func.count(func.distinct(Evidence.user_id))).where(
                Evidence.sha256.in_(digests), Evidence.user_id != row.user_id
            )
        )
        if others:
            warnings.append(
                f"The same receipt file was also uploaded by {others} other farmer"
                f"{'s' if others > 1 else ''}. Check that this payment is really this farmer's."
            )
    if kind == "momo":
        reference = row.reference or row.external_reference
    else:  # a farm record carries a MoMo ID only when it was paid by mobile money
        reference = row.external_reference if row.payment_method == "mobile_money" else None
    if reference:
        ref = reference.strip().upper()
        momo_users = db.scalars(
            select(func.distinct(MomoTransaction.user_id)).where(
                func.upper(MomoTransaction.reference) == ref, MomoTransaction.user_id != row.user_id
            )
        ).all()
        record_users = db.scalars(
            select(func.distinct(FarmRecord.user_id)).where(
                func.upper(FarmRecord.external_reference) == ref, FarmRecord.user_id != row.user_id
            )
        ).all()
        count = len(set(momo_users) | set(record_users))
        if count:
            warnings.append(
                f"MoMo transaction ID {reference.strip()} is also recorded by {count} other farmer"
                f"{'s' if count > 1 else ''}. Fine if one paid the other; otherwise the same payment "
                "is claimed twice."
            )
    return warnings


def review(db, actor, kind, record_id, decision, reason):
    row = entry(db, kind, record_id, lock=True)
    if decision == "verify" and not evidence_rows(db, kind, row.id):
        raise AppError("Evidence is required before verifying an entry.")
    next_status = transition(row.verification_status, decision, reason=reason)
    change_status(db, row, kind, next_status, actor_type="admin", actor=actor, reason=reason)
    append_audit(
        db,
        actor,
        action=f"transaction.{decision}",
        target_type="transaction",
        target_id=row.id,
        summary=f"Transaction {decision} decision",
        after={"kind": kind, "status": next_status, "reason": reason},
    )
    notice = notifications.create(
        db,
        row.user_id,
        "transaction",
        "Transaction review completed",
        f"Your entry is {'verified by AgriCtec' if decision == 'verify' else 'rejected'}. Reason: {reason}",
        "/records",
    )
    db.commit()
    notifications.deliver(notice)
    bus.publish("account", row.user_id)
    bus.publish("platform")
    return {"status": next_status}


def file(db, evidence_id, *, user_id=None, actor=None):
    evidence = db.get(Evidence, evidence_id)
    if evidence is None or (user_id is not None and evidence.user_id != user_id):
        raise NotFound("Evidence not found.")
    if actor is not None:
        links = db.scalars(select(Link).where(Link.evidence_id == evidence.id)).all()
        if not any(entry(db, l.record_kind, l.record_id).verification_status in QUEUE for l in links):
            raise NotFound("Evidence is not in the review queue.")
        append_audit(
            db,
            actor,
            action="transaction.evidence_view",
            target_type="evidence",
            target_id=evidence.id,
            summary="Viewed private transaction evidence",
        )
    found = store(db).get(evidence.file_key)
    if found is None:
        raise NotFound("Evidence file not found.")
    db.commit()
    return evidence.filename, found[0], found[1]


def status_counts(db, user_id):
    counts = Counter({s: 0 for s in STATUSES})
    for model in MODELS.values():
        counts.update(
            dict(
                db.execute(
                    select(model.verification_status, func.count())
                    .where(model.user_id == user_id)
                    .group_by(model.verification_status)
                ).all()
            )
        )
    return dict(counts)


def queue_count(db):
    return sum(
        db.scalar(select(func.count()).select_from(m).where(m.verification_status.in_(QUEUE))) or 0
        for m in MODELS.values()
    )
