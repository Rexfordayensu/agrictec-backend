"""The console's lending: loan applications, lenders and the credit rules."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.deps import AdminContext, require
from app.db import get_db
from app.domain.loans import lender_stats
from app.models import LoanApplication
from app.routers.common import file_response
from app.schemas import admin as a
from app.schemas.base import Message
from app.schemas.farm import LoanApplication as LoanOut
from app.schemas.platform import (
    CreditConfig,
    CreditConfigInput,
    LenderInput,
    LenderPatch,
    PublishCreditConfig,
    RestoreCreditConfig,
)
from app.schemas.platform import Lender as LenderOut
from app.services import admin_config, lending, platform_store

router = APIRouter(prefix="/admin")
Database = Annotated[DB, Depends(get_db)]


def allowed(permission: str):
    return Annotated[AdminContext, Depends(require(permission))]


# ---------- Loan applications ----------


@router.get("/loan-applications", response_model=list[LoanOut], tags=["Admin loans"])
def list_applications(
    ctx: allowed("loans.view"),
    db: Database,
    status: Annotated[str | None, Query(description="One or more statuses, comma-separated")] = None,
    partner_id: Annotated[str | None, Query(alias="partnerId")] = None,
    user_id: Annotated[str | None, Query(alias="userId")] = None,
    min_amount: Annotated[float | None, Query(alias="min")] = None,
    max_amount: Annotated[float | None, Query(alias="max")] = None,
    sent_from: Annotated[date | None, Query(alias="from")] = None,
    sent_to: Annotated[date | None, Query(alias="to")] = None,
    region: str | None = None,
):
    return lending.list_applications(
        db, status=status, partner_id=partner_id, user_id=user_id, min_amount=min_amount, max_amount=max_amount,
        sent_from=sent_from, sent_to=sent_to, region=region,
    )


@router.post("/loan-applications/export", tags=["Admin loans"])
def export_applications(body: a.IdList, ctx: allowed("reports.export"), db: Database):
    return file_response(lending.export_csv(db, ctx.admin, body.ids), "AgriCtec_Loan_Applications.csv")


@router.get("/loan-applications/{app_id}", response_model=LoanOut, tags=["Admin loans"])
def get_application(app_id: str, ctx: allowed("loans.view"), db: Database):
    return lending.get_application(db, app_id)


@router.get("/loan-applications/{app_id}/review", response_model=a.LoanReview, tags=["Admin loans"])
def review(app_id: str, ctx: allowed("loans.view"), db: Database):
    """Eligibility re-run with today's rules, and whether the farmer still exists."""
    return lending.review(db, app_id)


@router.post("/loan-applications/{app_id}/status", response_model=LoanOut, tags=["Admin loans"])
def update_status(app_id: str, body: a.StatusChange, ctx: allowed("loans.manage"), db: Database):
    """Allowed moves: submitted -> under_review | rejected; under_review -> approved | rejected;
    approved -> disbursed | rejected; disbursed -> repaid | defaulted."""
    return lending.update_status(db, ctx.admin, app_id, body)


# ---------- Lenders ----------


@router.get("/lenders", tags=["Admin lenders"])
def list_lenders(ctx: allowed("lenders.manage"), db: Database):
    """Every lender with its numbers: applications, under review, approval rate."""
    apps = list(db.scalars(select(LoanApplication)))
    return [
        {**lender.model_dump(mode="json", by_alias=True), "stats": lender_stats(apps, lender.id)}
        for lender in platform_store.lenders(db, include_inactive=True)
    ]


@router.post("/lenders", response_model=LenderOut, status_code=201, tags=["Admin lenders"])
def create_lender(body: LenderInput, ctx: allowed("lenders.manage"), db: Database):
    return lending.create_lender(db, ctx.admin, body)


@router.patch("/lenders/{lender_id}", response_model=LenderOut, tags=["Admin lenders"])
def update_lender(lender_id: str, body: LenderPatch, ctx: allowed("lenders.manage"), db: Database):
    return lending.update_lender(db, ctx.admin, lender_id, body)


@router.post("/lenders/{lender_id}/toggle", response_model=LenderOut, tags=["Admin lenders"])
def toggle_lender(lender_id: str, ctx: allowed("lenders.manage"), db: Database):
    """Not while applications are under review (no farmer is left waiting on a hidden lender)."""
    return lending.toggle_lender(db, ctx.admin, lender_id)


@router.delete("/lenders/{lender_id}", response_model=Message, tags=["Admin lenders"])
def delete_lender(lender_id: str, ctx: allowed("lenders.manage"), db: Database):
    """Only a lender with no applications at all: deactivate it instead."""
    lending.delete_lender(db, ctx.admin, lender_id)
    return Message(detail="Lender removed.")


# ---------- Credit rules ----------


@router.get("/credit-config/history", response_model=list[CreditConfig], tags=["Admin credit rules"])
def credit_history(ctx: allowed("credit.configure"), db: Database):
    """Every earlier version, newest first (the live one is GET /platform's creditConfig)."""
    return platform_store.credit_history(db)


@router.get("/credit-config/scoring-inputs", tags=["Admin credit rules"])
def scoring_inputs(ctx: allowed("credit.configure"), db: Database):
    """What the score needs for every registered farmer, so the console can preview a draft's
    impact as the admin types (nothing is saved)."""
    return [
        {
            "userId": i.user_id,
            "name": i.name,
            "region": i.region,
            "records": [r.model_dump(mode="json", by_alias=True) for r in i.records],
            "momo": [t.model_dump(mode="json", by_alias=True) for t in i.momo],
            "profile": i.profile.model_dump(mode="json", by_alias=True),
        }
        for i in admin_config.scoring_inputs(db)
    ]


@router.put("/credit-config", response_model=CreditConfig, tags=["Admin credit rules"])
def save_credit_config(body: CreditConfigInput, ctx: allowed("credit.configure"), db: Database):
    return admin_config.save_credit_config(db, ctx.admin, body)


@router.post("/credit-config/publish", response_model=CreditConfig, tags=["Admin credit rules"])
def publish_credit_config(body: PublishCreditConfig, ctx: allowed("credit.configure"), db: Database):
    """A new version with a change note (10+ characters); farmers' scores follow at once."""
    return admin_config.publish_credit_config(db, ctx.admin, body.config, body.note)


@router.post("/credit-config/restore", response_model=CreditConfig, tags=["Admin credit rules"])
def restore_credit_config(body: RestoreCreditConfig, ctx: allowed("credit.configure"), db: Database):
    """Copies an old version into a NEW version: history is never rewritten."""
    return admin_config.restore_credit_version(db, ctx.admin, body.version, body.note)


@router.post("/credit-config/impact", response_model=a.Impact, tags=["Admin credit rules"])
def credit_impact(body: a.ImpactRequest, ctx: allowed("credit.configure"), db: Database):
    """Every farmer re-scored under the live rules and a draft. Nothing is saved."""
    return admin_config.impact(db, body.draft)
