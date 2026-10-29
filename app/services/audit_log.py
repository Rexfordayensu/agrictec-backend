"""Read-only access to the audit trail (services/admin/audit.service.ts). There is deliberately
no update or delete here, or anywhere."""

import json
from datetime import UTC, datetime, time

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError
from app.domain.clock import iso, now_utc
from app.domain.csv import to_csv
from app.domain.permissions import AUTH_ACTIONS
from app.models import Admin, AuditEntry
from app.schemas.admin import AuditActor, AuditFilter
from app.schemas.admin import AuditEntry as AuditOut
from app.schemas.base import Paged
from app.services.mappers import audit_out

PAGE_SIZE = 50
GROUP_PREFIXES = {
    "farmer": ["farmer", "verification", "risk"],
    "loan": ["loan", "lender"],
    "config": ["credit"],
    "content": ["content", "announcement"],
    "team": ["admin"],
}


def _bound(value: str, end: bool) -> datetime:
    """An ISO timestamp, or a "yyyy-MM-dd" day (an upper bound includes that whole day)."""
    try:
        if len(value) == 10:
            day = datetime.fromisoformat(value).date()
            return datetime.combine(day, time.max if end else time.min, UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError as error:
        raise AppError(f"“{value}” is not a date.") from error


def _conditions(flt: AuditFilter) -> list:
    c = []
    if flt.actor_id:
        c.append(AuditEntry.actor_id == flt.actor_id)
    if flt.action:
        c.append(AuditEntry.action.startswith(flt.action) if flt.action.endswith(".") else AuditEntry.action == flt.action)
    if flt.group:
        prefix = func.split_part(AuditEntry.action, ".", 1)
        if flt.group == "auth":
            c.append(AuditEntry.action.in_(AUTH_ACTIONS))
        elif flt.group == "system":
            known = [p for prefixes in GROUP_PREFIXES.values() for p in prefixes]
            c.append(and_(AuditEntry.action.not_in(AUTH_ACTIONS), prefix.not_in(known)))
        else:
            c.append(and_(AuditEntry.action.not_in(AUTH_ACTIONS), prefix.in_(GROUP_PREFIXES[flt.group])))
    if flt.target_type:
        c.append(AuditEntry.target_type == flt.target_type)
    if flt.target_id:
        c.append(AuditEntry.target_id == flt.target_id)
    if flt.from_:
        c.append(AuditEntry.created_at >= _bound(flt.from_, end=False))
    if flt.to:
        c.append(AuditEntry.created_at <= _bound(flt.to, end=True))
    if flt.search and flt.search.strip():
        term = f"%{flt.search.strip()}%"
        c.append(
            or_(
                AuditEntry.summary.ilike(term),
                AuditEntry.actor_name.ilike(term),
                AuditEntry.target_id.ilike(term),
                AuditEntry.action.ilike(term),
            )
        )
    return c


def list_entries(db: DB, flt: AuditFilter, page: int = 1, page_size: int = PAGE_SIZE) -> Paged[AuditOut]:
    """Newest first, one page at a time."""
    page_size = max(1, min(page_size, 500))
    conditions = _conditions(flt)
    total = db.scalar(select(func.count()).select_from(AuditEntry).where(*conditions)) or 0
    page_count = max(1, -(-total // page_size))
    current = min(max(1, page), page_count)
    rows = db.scalars(
        select(AuditEntry)
        .where(*conditions)
        .order_by(AuditEntry.seq.desc())
        .offset((current - 1) * page_size)
        .limit(page_size)
    )
    return Paged[AuditOut](
        items=[audit_out(r) for r in rows], total=total, page=current, page_size=page_size, page_count=page_count
    )


def actors(db: DB) -> list[AuditActor]:
    """The people who appear in the log, most recent activity first."""
    latest = (
        select(AuditEntry.actor_id, func.max(AuditEntry.seq).label("last"))
        .group_by(AuditEntry.actor_id)
        .subquery()
    )
    rows = db.execute(
        select(AuditEntry.actor_id, AuditEntry.actor_name, AuditEntry.actor_role)
        .join(latest, and_(AuditEntry.actor_id == latest.c.actor_id, AuditEntry.seq == latest.c.last))
        .order_by(latest.c.last.desc())
    )
    return [AuditActor(id=r.actor_id, name=r.actor_name, role=r.actor_role) for r in rows]


def export(db: DB, actor: Admin, flt: AuditFilter, fmt: str) -> dict:
    """Every matching entry, oldest first, as JSON or CSV. The export itself is logged."""
    entries = [audit_out(r) for r in db.scalars(select(AuditEntry).where(*_conditions(flt)).order_by(AuditEntry.seq))]
    now = now_utc()
    filter_json = flt.model_dump(mode="json", by_alias=True, exclude_none=True)
    if fmt == "json":
        content = json.dumps(
            {
                "app": "agrictec",
                "kind": "audit-log",
                "exportedAt": iso(now),
                "filter": filter_json,
                "entries": [e.model_dump(mode="json", by_alias=True) for e in entries],
            },
            indent=2,
            ensure_ascii=False,
        )
    else:
        content = to_csv(
            entries,
            [
                ("Time", lambda e: iso(e.created_at)),
                ("Actor", lambda e: e.actor_name),
                ("Role", lambda e: e.actor_role),
                ("Action", lambda e: e.action),
                ("Target type", lambda e: e.target_type),
                ("Target", lambda e: e.target_id or ""),
                ("Summary", lambda e: e.summary),
                ("Before", lambda e: "" if e.before is None else json.dumps(e.before, ensure_ascii=False)),
                ("After", lambda e: "" if e.after is None else json.dumps(e.after, ensure_ascii=False)),
            ],
        )
    count = len(entries)
    append_audit(
        db, actor, action="audit.export", target_type="system",
        summary=f"Exported {count} audit entr{'y' if count == 1 else 'ies'} ({fmt.upper()})",
        after={"count": count, "format": fmt, **filter_json},
    )
    db.commit()
    return {"filename": f"AgriCtec_Audit_Log_{iso(now)[:10]}.{fmt}", "content": content}
