"""The audit trail (admin spec Section 8).

append_audit adds one entry to the same database transaction as the change it describes, so a
change and its entry are committed together or not at all. Nothing edits or deletes an entry,
and a trigger in the database refuses UPDATE, DELETE and TRUNCATE on audit_log.

Secrets never enter the log: services pass summaries (counts, masked values), never temporary
passwords, password hashes or full Ghana Card numbers."""

from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from sqlalchemy.orm import Session as DB

from app.domain.clock import now_utc
from app.models import Admin, AuditEntry


def to_json(value: Any) -> Any:
    """A JSON copy of a value (models in their camelCase API shape)."""
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list | tuple):
        return [to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    return to_jsonable_python(value)


def append_audit(
    db: DB,
    actor: Admin,
    *,
    action: str,
    target_type: str,
    summary: str,
    target_id: str | None = None,
    before: Any = None,
    after: Any = None,
) -> AuditEntry:
    entry = AuditEntry(
        actor_id=actor.id,
        actor_name=actor.name,
        actor_role=actor.role,
        action=action,
        target_type=target_type,
        target_id=target_id,
        summary=summary,
        before=to_json(before),
        after=to_json(after),
        created_at=now_utc(),
    )
    db.add(entry)
    return entry
