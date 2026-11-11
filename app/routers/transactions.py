"""Authenticated transaction history and review queue; no general admin browsing endpoint."""

from datetime import date
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.deps import AdminContext, FarmerContext, farmer_context, require, writable_farmer
from app.core.ratelimit import rate_limit
from app.db import get_db
from app.schemas.base import CamelModel, Text
from app.services import transactions as service

router = APIRouter(tags=["Transaction history"])
Reader = Annotated[FarmerContext, Depends(farmer_context)]
Writer = Annotated[FarmerContext, Depends(writable_farmer)]
Reviewer = Annotated[AdminContext, Depends(require("transactions.review"))]
Database = Annotated[Session, Depends(get_db)]
Kind = Literal["farm", "momo"]
Status = Literal["self_reported", "evidence_submitted", "verification_pending", "verified", "rejected", "disputed"]


class Reason(CamelModel):
    reason: Text(1000)


@router.get("/me/transactions")
def history(
    ctx: Reader,
    db: Database,
    kind: Kind | None = None,
    type: Literal["income", "expense", "activity", "financing", "in", "out"] | None = None,
    category: str | None = None,
    verification_status: Status | None = None,
    source_type: Literal["farmer_entry", "statement_upload"] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    return service.list_history(
        db,
        ctx.user.id,
        kind=kind,
        type=type,
        category=category,
        verification_status=verification_status,
        source_type=source_type,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@router.get("/me/transactions/summary")
def summary(ctx: Reader, db: Database, months: Literal["3", "6", "12"] = "3"):
    return service.summary(db, ctx.user.id, int(months))


@router.post(
    "/me/transactions/{kind}/{record_id}/evidence", dependencies=[Depends(rate_limit("transaction-evidence", 30, 3600))]
)
def upload(
    kind: Kind,
    record_id: str,
    ctx: Writer,
    db: Database,
    file: Annotated[UploadFile, File()],
    evidence_kind: Annotated[Literal["receipt", "statement", "other"], Form()] = "receipt",
):
    return service.attach(
        db, ctx.user.id, kind, record_id, file.file.read(service.MAX_BYTES + 1), file.filename, evidence_kind
    )


@router.delete("/me/evidence/{evidence_id}", status_code=204)
def remove_evidence(evidence_id: str, ctx: Writer, db: Database):
    service.remove_evidence(db, ctx.user.id, evidence_id)


@router.post("/me/transactions/{kind}/{record_id}/request-review")
def request_review(kind: Kind, record_id: str, ctx: Writer, db: Database):
    return service.farmer_action(db, ctx.user.id, kind, record_id, "request_review")


@router.post("/me/transactions/{kind}/{record_id}/dispute")
def dispute(kind: Kind, record_id: str, body: Reason, ctx: Writer, db: Database):
    return service.farmer_action(db, ctx.user.id, kind, record_id, "dispute", body.reason)


def attachment(found):
    name, data, content_type = found
    return Response(
        data,
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/me/evidence/{evidence_id}/file")
def own_file(evidence_id: str, ctx: Reader, db: Database):
    return attachment(service.file(db, evidence_id, user_id=ctx.user.id))


@router.get("/admin/transactions/review")
def review_queue(ctx: Reviewer, db: Database, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    return service.review_queue(db, ctx.admin, page, page_size)


@router.get("/admin/transactions/evidence/{evidence_id}/file")
def review_file(evidence_id: str, ctx: Reviewer, db: Database):
    return attachment(service.file(db, evidence_id, actor=ctx.admin))


@router.post("/admin/transactions/{kind}/{record_id}/verify")
def verify(kind: Kind, record_id: str, body: Reason, ctx: Reviewer, db: Database):
    return service.review(db, ctx.admin, kind, record_id, "verify", body.reason)


@router.post("/admin/transactions/{kind}/{record_id}/reject")
def reject(kind: Kind, record_id: str, body: Reason, ctx: Reviewer, db: Database):
    return service.review(db, ctx.admin, kind, record_id, "reject", body.reason)


@router.get("/me/transactions/{kind}/{record_id}")
def own_transaction(kind: Kind, record_id: str, ctx: Reader, db: Database):
    return service.item(db, service.entry(db, kind, record_id, ctx.user.id), kind)


@router.get("/admin/transactions/review/count")
def queue_count(ctx: Reviewer, db: Database):
    return {"count": service.queue_count(db)}
