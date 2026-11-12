"""Preliminary loan eligibility for one farmer and one published loan product
(app/domain/loan_assessment.py does the analysis; this loads the data and keeps a snapshot).

The farmer is always the signed-in one: nothing here takes a farmer ID from a request, so no
farmer can see another's figures. Every check is saved with the product version it used."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session as DB

from app.core.errors import Forbidden
from app.domain.clock import iso, now_utc, today
from app.domain.credit import is_profile_complete
from app.domain.loan_assessment import OUTCOME_LABELS, Facts, evaluate
from app.models import FarmRecord, LoanEligibilityAssessment, MomoTransaction
from app.services import farm, loan_products, platform_store
from app.services import ghana_card_verification as identity


def facts_for(db: DB, user_id: str) -> Facts:
    profile = farm.get_profile(db, user_id)
    return Facts(
        identity_level=identity.identity_level(db, user_id),
        phone_verified=bool(profile.verification.phone),
        location_verified=bool(profile.verification.location),
        profile_complete=is_profile_complete(profile),
        cooperative_member=profile.farm_type == "Cooperative" or bool((profile.cooperative_name or "").strip()),
        acres=Decimal(str(profile.acres)) if profile.acres else None,
        region=profile.region or None,
        crops=list(profile.primary_crops or []),
        farm_type=profile.farm_type or None,
    )


def check(db: DB, user_id: str, product_id: str, amount: Decimal, months: int) -> dict:
    if not platform_store.flags(db).loans:
        raise Forbidden("Loans are currently unavailable.", code="feature_off")
    product = loan_products.listed_product(db, product_id)  # 404 unless farmers can see it
    farm_rows = db.scalars(select(FarmRecord).where(FarmRecord.user_id == user_id)).all()
    momo_rows = db.scalars(select(MomoTransaction).where(MomoTransaction.user_id == user_id)).all()
    result = evaluate(
        product=product.model_dump(),
        lender_name=product.lender.name,
        amount=amount,
        months=months,
        facts=facts_for(db, user_id),
        farm=list(farm_rows),
        momo=list(momo_rows),
        today=today(),
    )
    result["product"] = {
        "id": product.id, "name": product.name, "version": product.version,
        "lender": {"id": product.lender.id, "name": product.lender.name},
        "source": product.source,
        "sourceCheckedOn": product.source_checked_on.isoformat() if product.source_checked_on else None,
    }
    result["identityLevel"] = identity.identity_level(db, user_id)
    row = LoanEligibilityAssessment(
        user_id=user_id, product_id=product.id, product_version=product.version, lender_id=product.lender.id,
        amount=amount, months=months, outcome=result["outcome"], result=result, created_at=now_utc(),
    )
    db.add(row)
    db.commit()
    return {**result, "id": row.id, "createdAt": iso(row.created_at)}


def latest(db: DB, user_id: str, limit: int = 10) -> list[dict]:
    """The farmer's most recent check of each product (newest first), for the readiness view."""
    rows = db.scalars(
        select(LoanEligibilityAssessment)
        .where(LoanEligibilityAssessment.user_id == user_id)
        .order_by(LoanEligibilityAssessment.created_at.desc())
        .limit(200)
    ).all()
    seen: set[str] = set()
    out = []
    for row in rows:
        if row.product_id in seen:
            continue
        seen.add(row.product_id)
        product = row.result.get("product") or {}
        out.append({
            "id": row.id, "productId": row.product_id, "productVersion": row.product_version,
            "productName": product.get("name"), "lenderName": (product.get("lender") or {}).get("name"),
            "outcome": row.outcome, "outcomeLabel": OUTCOME_LABELS[row.outcome],
            "summary": row.result.get("summary"), "amount": f"{row.amount:.2f}", "months": row.months,
            "createdAt": iso(row.created_at),
        })
        if len(out) >= limit:
            break
    return out
