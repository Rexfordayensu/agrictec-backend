"""The console's lending: loan applications, lenders and the credit rules."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from sqlalchemy.orm import Session as DB

from app.core.deps import AdminContext, require
from app.core.errors import NotFound
from app.db import get_db
from app.routers.common import file_response
from app.schemas import admin as a
from app.schemas import products
from app.schemas.base import Message
from app.schemas.farm import LoanApplication as LoanOut
from app.schemas.platform import (
    AdminLender,
    CreditConfig,
    CreditConfigInput,
    LenderInput,
    LenderListing,
    LenderPatch,
    LenderSuspension,
    LenderVerification,
    PublishCreditConfig,
    RestoreCreditConfig,
)
from app.services import admin_config, lending, loan_products, platform_store

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
# Add (draft) -> record the licence check -> authorise the listing -> publish. Suspend hides a
# lender from every farmer at once. Every step is audited.

Lenders = allowed("lenders.manage")


@router.get("/lenders", response_model=list[AdminLender], tags=["Admin lenders"])
def list_lenders(ctx: Lenders, db: Database):
    """Every lender, with whether farmers see it and, if not, why."""
    return lending.list_admin_lenders(db)


@router.get("/lenders/{lender_id}", response_model=AdminLender, tags=["Admin lenders"])
def get_lender(lender_id: str, ctx: Lenders, db: Database):
    return lending.get_admin_lender(db, lender_id)


@router.post("/lenders", response_model=AdminLender, status_code=201, tags=["Admin lenders"])
def create_lender(body: LenderInput, ctx: Lenders, db: Database):
    """Starts as an unverified draft that farmers don't see."""
    return lending.create_lender(db, ctx.admin, body)


@router.patch("/lenders/{lender_id}", response_model=AdminLender, tags=["Admin lenders"])
def update_lender(lender_id: str, body: LenderPatch, ctx: Lenders, db: Database):
    """Changing the name, registered name, type, licence or regulator clears the verification."""
    return lending.update_lender(db, ctx.admin, lender_id, body)


@router.post("/lenders/{lender_id}/verification", response_model=AdminLender, tags=["Admin lenders"])
def verify_lender(lender_id: str, body: LenderVerification, ctx: Lenders, db: Database):
    """The licence check against an official source (required unless 'unverified')."""
    return lending.verify_lender(db, ctx.admin, lender_id, body)


@router.post("/lenders/{lender_id}/listing", response_model=AdminLender, tags=["Admin lenders"])
def set_listing(lender_id: str, body: LenderListing, ctx: Lenders, db: Database):
    return lending.set_listing(db, ctx.admin, lender_id, body)


@router.post("/lenders/{lender_id}/publish", response_model=AdminLender, tags=["Admin lenders"])
def publish_lender(lender_id: str, ctx: Lenders, db: Database):
    """Only with a current licence check, its source and an authorised listing."""
    return lending.publish_lender(db, ctx.admin, lender_id)


@router.post("/lenders/{lender_id}/unpublish", response_model=AdminLender, tags=["Admin lenders"])
def unpublish_lender(lender_id: str, ctx: Lenders, db: Database):
    return lending.unpublish_lender(db, ctx.admin, lender_id)


@router.post("/lenders/{lender_id}/suspend", response_model=AdminLender, tags=["Admin lenders"])
def suspend_lender(lender_id: str, body: LenderSuspension, ctx: Lenders, db: Database):
    return lending.suspend_lender(db, ctx.admin, lender_id, body)


@router.post("/lenders/{lender_id}/reinstate", response_model=AdminLender, tags=["Admin lenders"])
def reinstate_lender(lender_id: str, ctx: Lenders, db: Database):
    return lending.reinstate_lender(db, ctx.admin, lender_id)


@router.delete("/lenders/{lender_id}", response_model=Message, tags=["Admin lenders"])
def delete_lender(lender_id: str, ctx: Lenders, db: Database):
    """Only a lender with no applications at all: unpublish or suspend it instead."""
    lending.delete_lender(db, ctx.admin, lender_id)
    return Message(detail="Lender removed.")


@router.put("/lenders/{lender_id}/logo", response_model=AdminLender, tags=["Admin lenders"])
def upload_logo(lender_id: str, ctx: Lenders, db: Database, file: Annotated[UploadFile, File()]):
    """The institution's genuine logo: PNG, JPEG or WebP, 2 MB at most."""
    return lending.set_logo(db, ctx.admin, lender_id, file.file.read(lending.LOGO_MAX_BYTES + 1))


@router.delete("/lenders/{lender_id}/logo", response_model=AdminLender, tags=["Admin lenders"])
def remove_logo(lender_id: str, ctx: Lenders, db: Database):
    return lending.remove_logo(db, ctx.admin, lender_id)


@router.get("/lenders/{lender_id}/logo", include_in_schema=False)
def lender_logo(lender_id: str, ctx: Lenders, db: Database):
    """Any lender's logo, published or not (for the console)."""
    data, media_type = lending.logo(db, lender_id, listed_only=False)
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


# ---------- Loan products ----------
# A lender's products: draft -> publish (a frozen, numbered version) -> unpublish. Terms and
# requirements come from the lender's official information, with their source.


@router.get("/lenders/{lender_id}/products", response_model=list[products.AdminProduct], tags=["Admin loan products"])
def list_products(lender_id: str, ctx: Lenders, db: Database):
    return loan_products.list_for_lender(db, lender_id)


@router.post("/lenders/{lender_id}/products", response_model=products.AdminProduct, status_code=201,
             tags=["Admin loan products"])
def create_product(lender_id: str, body: products.NewProduct, ctx: Lenders, db: Database):
    """Starts as an empty draft that farmers don't see."""
    return loan_products.create(db, ctx.admin, lender_id, body)


@router.get("/loan-products/{product_id}", response_model=products.AdminProduct, tags=["Admin loan products"])
def get_product(product_id: str, ctx: Lenders, db: Database):
    return loan_products.get_admin(db, product_id)


@router.put("/loan-products/{product_id}/draft", response_model=products.AdminProduct, tags=["Admin loan products"])
def save_product_draft(product_id: str, body: products.ProductContent, ctx: Lenders, db: Database):
    """Saved as typed; `problems` lists what must be fixed before publishing."""
    return loan_products.save_draft(db, ctx.admin, product_id, body)


@router.post("/loan-products/{product_id}/draft/reset", response_model=products.AdminProduct,
             tags=["Admin loan products"])
def reset_product_draft(product_id: str, ctx: Lenders, db: Database):
    return loan_products.reset_draft(db, ctx.admin, product_id)


@router.post("/loan-products/{product_id}/publish", response_model=products.AdminProduct,
             tags=["Admin loan products"])
def publish_product(product_id: str, body: products.PublishProduct, ctx: Lenders, db: Database):
    """Freezes the draft into a new read-only version that farmers see (if its lender is listed)."""
    return loan_products.publish(db, ctx.admin, product_id, body)


@router.post("/loan-products/{product_id}/unpublish", response_model=products.AdminProduct,
             tags=["Admin loan products"])
def unpublish_product(product_id: str, ctx: Lenders, db: Database):
    return loan_products.unpublish(db, ctx.admin, product_id)


@router.delete("/loan-products/{product_id}", response_model=Message, tags=["Admin loan products"])
def delete_product(product_id: str, ctx: Lenders, db: Database):
    """Only a product that was never published."""
    loan_products.delete(db, ctx.admin, product_id)
    return Message(detail="Loan product removed.")


@router.get("/loan-products/{product_id}/versions", response_model=list[products.ProductVersion],
            tags=["Admin loan products"])
def product_versions(product_id: str, ctx: Lenders, db: Database):
    """Every published version, newest first."""
    return loan_products.versions(db, product_id)


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


@router.get("/credit-config/scoring-inputs/{user_id}", tags=["Admin credit rules"])
def scoring_input(user_id: str, ctx: allowed("credit.configure"), db: Database):
    """What the score needs for ONE farmer: the simulator tries the draft on them (the impact
    on everyone is worked out here, by POST /credit-config/impact)."""
    found = admin_config.scoring_inputs(db, [user_id])
    if not found:
        raise NotFound("Farmer not found.")
    i = found[0]
    return {
        "userId": i.user_id,
        "name": i.name,
        "region": i.region,
        "records": [r.model_dump(mode="json", by_alias=True) for r in i.records],
        "momo": [t.model_dump(mode="json", by_alias=True) for t in i.momo],
        "profile": i.profile.model_dump(mode="json", by_alias=True),
    }


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
