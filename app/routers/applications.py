"""Loan applications (app/services/loan_applications.py): the farmer prepares and submits;
AgriCtec reviews, sends to the lender, and records the lender's progress with its proof."""

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import Field

from app.core.deps import AdminContext, FarmerContext, farmer_context, require, writable_farmer
from app.core.ratelimit import rate_limit
from app.db import get_db
from app.routers.transactions import attachment
from app.schemas.base import CamelModel
from app.services import financial_reports
from app.services import loan_applications as apps
from sqlalchemy.orm import Session as DB

farmer_router = APIRouter(prefix="/me/applications", tags=["Loan applications"])
admin_router = APIRouter(prefix="/admin/applications", tags=["Admin applications"])

Reader = Annotated[FarmerContext, Depends(farmer_context)]
Writer = Annotated[FarmerContext, Depends(writable_farmer)]
Database = Annotated[DB, Depends(get_db)]
MAX_UPLOAD = 6 * 1024 * 1024


def allowed(permission: str):
    return Annotated[AdminContext, Depends(require(permission))]


Amount = Annotated[Decimal, Field(gt=0, le=100_000_000, max_digits=14, decimal_places=2)]
Months = Annotated[int, Field(ge=1, le=360)]


class NewApplication(CamelModel):
    product_id: str
    amount: Amount
    months: Months
    purpose: Annotated[str, Field(min_length=3, max_length=300)]


class ApplicationChanges(CamelModel):
    amount: Amount | None = None
    months: Months | None = None
    purpose: Annotated[str | None, Field(min_length=3, max_length=300)] = None
    letter: Annotated[str | None, Field(max_length=4000)] = None
    answers: dict[str, str | None] | None = None


class Submission(CamelModel):
    consent: bool
    notice_version: str


class Note(CamelModel):
    note: Annotated[str | None, Field(max_length=1000)] = None


class Delivery(CamelModel):
    how: Annotated[str, Field(min_length=5, max_length=500)]


async def _read(upload: UploadFile) -> bytes:
    data = await upload.read(MAX_UPLOAD + 1)
    return data[: MAX_UPLOAD + 1]


# ---------- Farmer ----------


@farmer_router.get("")
def list_mine(ctx: Reader, db: Database):
    return apps.list_for(db, ctx.user.id)


@farmer_router.get("/consent-notice")
def consent_notice():
    """What a farmer agrees to share when submitting (its version is sent back with consent)."""
    return {"version": apps.CONSENT_VERSION, "dataItems": apps.DATA_ITEMS}


@farmer_router.post("", status_code=201, dependencies=[Depends(rate_limit("application-create", 30, 3600))])
def start(body: NewApplication, ctx: Writer, db: Database):
    return apps.create(db, ctx.user.id, body.product_id, body.amount, body.months, body.purpose)


@farmer_router.get("/{app_id}")
def get_mine(app_id: str, ctx: Reader, db: Database):
    return apps.get(db, ctx.user.id, app_id)


@farmer_router.patch("/{app_id}")
def change(app_id: str, body: ApplicationChanges, ctx: Writer, db: Database):
    return apps.update(db, ctx.user.id, app_id, body.model_dump(exclude_unset=True))


@farmer_router.post("/{app_id}/documents", dependencies=[Depends(rate_limit("application-upload", 60, 3600))])
async def upload(app_id: str, ctx: Writer, db: Database, requirement_key: Annotated[str, Form(alias="requirementKey")],
                 file: Annotated[UploadFile, File()]):
    return apps.add_document(db, ctx.user.id, app_id, requirement_key, await _read(file), file.filename or "")


@farmer_router.delete("/{app_id}/documents/{document_id}")
def remove(app_id: str, document_id: str, ctx: Writer, db: Database):
    return apps.remove_document(db, ctx.user.id, app_id, document_id)


@farmer_router.get("/{app_id}/documents/{document_id}/file")
def my_document(app_id: str, document_id: str, ctx: Reader, db: Database):
    data, content_type, name = apps.own_document(db, ctx.user.id, app_id, document_id)
    return attachment((name, data, content_type))


@farmer_router.get("/{app_id}/pack")
def my_pack(app_id: str, ctx: Reader, db: Database):
    app = apps.get(db, ctx.user.id, app_id)
    return attachment((f"{app['reference']}-application.pdf", apps.own_pack(db, ctx.user.id, app_id), "application/pdf"))


@farmer_router.post("/{app_id}/submit", dependencies=[Depends(rate_limit("application-submit", 20, 3600))])
def submit(app_id: str, body: Submission, ctx: Writer, db: Database):
    return apps.submit(db, ctx.user.id, app_id, consent=body.consent, notice_version=body.notice_version)


@farmer_router.post("/{app_id}/withdraw")
def withdraw(app_id: str, body: Note, ctx: Writer, db: Database):
    return apps.withdraw(db, ctx.user.id, app_id, body.note)


# ---------- Admin ----------


@admin_router.get("")
def admin_list(ctx: allowed("loans.view"), db: Database,
               group: Annotated[Literal["to_review", "ready_to_send", "with_lender", "returned", "closed"] | None,
                                Query()] = None):
    """Submitted applications (drafts stay private to the farmer)."""
    return apps.admin_list(db, group)


@admin_router.get("/counts")
def admin_counts(ctx: allowed("loans.view"), db: Database):
    return apps.counts(db)


@admin_router.get("/{app_id}")
def admin_get(app_id: str, ctx: allowed("loans.view"), db: Database):
    """The application, the farmer's identity level, and whether its figures tally with the
    farmer's records now. Every view is written to the audit log."""
    return apps.admin_get(db, ctx.admin, app_id)


@admin_router.get("/{app_id}/documents/{document_id}/file")
def admin_document(app_id: str, document_id: str, ctx: allowed("loans.view"), db: Database):
    data, content_type, name = apps.admin_document(db, ctx.admin, app_id, document_id)
    return attachment((name, data, content_type))


@admin_router.get("/{app_id}/pack")
def admin_pack(app_id: str, ctx: allowed("loans.view"), db: Database):
    return attachment((f"application-{app_id[:8]}.pdf", apps.admin_pack(db, ctx.admin, app_id), "application/pdf"))


@admin_router.get("/{app_id}/statement")
def admin_statement(app_id: str, ctx: allowed("loans.view"), db: Database):
    app = apps.admin_get(db, ctx.admin, app_id)
    if not app["statementReportId"]:
        from app.core.errors import NotFound

        raise NotFound("No statement attached.")
    data, name = financial_reports.admin_file(db, ctx.admin, app["userId"], app["statementReportId"])
    return attachment((name, data, "application/pdf"))


@admin_router.get("/{app_id}/events/{event_id}/proof")
def admin_proof(app_id: str, event_id: int, ctx: allowed("loans.view"), db: Database):
    data, content_type, name = apps.admin_proof(db, ctx.admin, app_id, event_id)
    return attachment((name, data, content_type))


@admin_router.post("/{app_id}/review")
def start_review(app_id: str, ctx: allowed("loans.manage"), db: Database):
    return apps.start_review(db, ctx.admin, app_id)


@admin_router.post("/{app_id}/request-changes")
def request_changes(app_id: str, body: Note, ctx: allowed("loans.manage"), db: Database):
    return apps.request_changes(db, ctx.admin, app_id, body.note or "")


@admin_router.post("/{app_id}/approve")
def approve(app_id: str, body: Note, ctx: allowed("loans.manage"), db: Database):
    """AgriCtec's review passed: send it (or wait as ready to send). Not a loan approval."""
    return apps.approve(db, ctx.admin, app_id, body.note)


@admin_router.post("/{app_id}/send")
def send(app_id: str, ctx: allowed("loans.manage"), db: Database):
    return apps.send(db, ctx.admin, app_id)


@admin_router.post("/{app_id}/record-sent")
def record_sent(app_id: str, body: Delivery, ctx: allowed("loans.manage"), db: Database):
    return apps.record_sent(db, ctx.admin, app_id, body.how)


@admin_router.post("/{app_id}/lender-status")
async def lender_status(
    app_id: str, ctx: allowed("loans.manage"), db: Database,
    status: Annotated[str, Form()], note: Annotated[str, Form()], proof: Annotated[UploadFile, File()],
):
    """A lender's progress, only with its confirmation (email or letter) attached."""
    return apps.record_lender_status(db, ctx.admin, app_id, status, note, await _read(proof), proof.filename or "")
