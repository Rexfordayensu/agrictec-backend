"""Plans, entitlements and AI credits (services/subscription.service.ts), now enforced by the
server. Payments are still simulated ("mock" mode): connect a payment provider and verify each
payment server-side before activating a paid plan."""

import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.errors import AppError, Forbidden
from app.domain.clock import now_utc
from app.domain.plans import (
    AI_COSTS,
    FEATURE_NAMES,
    credits_limit,
    month_key,
    next_billing_date,
    next_month_start,
    plan_access,
)
from app.integrations import paystack
from app.models import CreditReservation, Payment, Subscription
from app.schemas.farm import MockPayment, SubscriptionSnapshot, SubscriptionState
from app.services import platform_store

# A hold nobody settled (a crashed request) gives its credits back after this long.
HOLD_MINUTES = 10


def _state(db: DB, user_id: str, now: datetime) -> SubscriptionState:
    row = db.get(Subscription, user_id)
    mode = row.mode if row and row.mode in ("mock", "paystack") else "mock"
    if row is None or row.plan_id == "free":
        return SubscriptionState(
            user_id=user_id, plan_id="free", mode=mode, starts_at=None, expires_at=None,
            payment=row.payment if row else None,
        )
    if not row.starts_at or not row.expires_at or row.starts_at > now or row.expires_at <= now:
        # Expired: back to Free (the last receipt stays visible). No automatic renewal.
        return SubscriptionState(
            user_id=user_id, plan_id="free", mode=mode, starts_at=None, expires_at=None, payment=row.payment
        )
    return SubscriptionState(
        user_id=user_id, plan_id=row.plan_id, mode=mode, starts_at=row.starts_at,
        expires_at=row.expires_at, payment=row.payment,
    )


def _used(db: DB, user_id: str, now: datetime) -> int:
    """Settled credits this month plus holds still in flight."""
    return int(
        db.scalar(
            select(func.coalesce(func.sum(CreditReservation.cost), 0)).where(
                CreditReservation.user_id == user_id,
                CreditReservation.month == month_key(now),
                (CreditReservation.status == "settled")
                | (
                    (CreditReservation.status == "pending")
                    & (CreditReservation.created_at > now - timedelta(minutes=HOLD_MINUTES))
                ),
            )
        )
        or 0
    )


def snapshot(db: DB, user_id: str, now: datetime | None = None) -> SubscriptionSnapshot:
    now = now or now_utc()
    state = _state(db, user_id, now)
    limit = credits_limit(platform_store.plan(db, state.plan_id))
    used = _used(db, user_id, now)
    return SubscriptionSnapshot(
        subscription=state,
        credits_used=used,
        credits_limit=limit,
        credits_remaining=None if limit is None else max(0, limit - used),
        resets_at=next_month_start(now),
    )


def assert_feature(db: DB, user_id: str, feature: str, now: datetime | None = None) -> SubscriptionSnapshot:
    """Refuses a tool the admin switched off, or one the farmer's plan doesn't include."""
    name = FEATURE_NAMES[feature]
    if not platform_store.flags(db).model_dump(by_alias=True)[feature]:
        raise Forbidden(f"{name} is currently unavailable.", code="feature_off")
    snap = snapshot(db, user_id, now)
    plan = platform_store.plan(db, snap.subscription.plan_id)
    access = plan_access(plan).model_dump(by_alias=True)
    if not access[feature]:
        raise Forbidden(
            f"{name} is not included in {plan.name}. View plans in Settings → Plans & billing.", code="plan_required"
        )
    return snap


def _available_plan(db: DB, plan_id: str):
    plan = next((p for p in platform_store.plans(db) if p.id == plan_id), None)
    if plan is None or not plan.active:
        raise AppError("This plan is not available. Choose another plan.")
    return plan


def activate(db: DB, user_id: str, plan_id: str, method: str) -> SubscriptionSnapshot:
    """Switches to Free, or (DEMO only) activates a paid plan with a simulated payment. With
    Paystack configured, and always in production, paid plans go through checkout() instead.
    Switching or renewing never resets this month's used credits."""
    now = now_utc()
    plan = _available_plan(db, plan_id)
    settings = get_settings()
    if plan_id != "free" and (settings.payments_live or settings.environment == "production"):
        raise Forbidden("Pay for this plan through checkout.", code="payment_required")
    row = db.get(Subscription, user_id) or Subscription(user_id=user_id)
    if plan_id == "free":
        row.plan_id, row.starts_at, row.expires_at = "free", None, None
    else:
        row.plan_id, row.starts_at, row.expires_at = plan_id, now, next_billing_date(now)
        row.payment = MockPayment(
            reference=f"DEMO-{uuid.uuid4()}", plan_id=plan_id, amount_ghs=plan.price_ghs, method=method, created_at=now
        ).model_dump(mode="json", by_alias=True)
    row.mode = "mock"
    db.merge(row)
    db.commit()
    return snapshot(db, user_id, now)


# ---------- Real payments (Paystack) ----------


def checkout(db: DB, user, plan_id: str, method: str) -> dict:
    """Starts a Paystack payment for a paid plan; returns its checkout page."""
    settings = get_settings()
    if not settings.payments_live:
        raise AppError("Online payments are not set up yet.", code="payments_off")
    if plan_id == "free":
        raise AppError("Free needs no payment.")
    if user.is_guest:
        raise Forbidden("Create an account to subscribe to a paid plan.", code="account_required")
    plan = _available_plan(db, plan_id)
    reference = f"AGT-{secrets.token_hex(8).upper()}"
    payment = Payment(
        reference=reference, user_id=user.id, plan_id=plan_id, amount=plan.price_ghs, method=method,
        status="pending", created_at=now_utc(),
    )
    db.add(payment)
    db.commit()
    try:
        url = paystack.initialize(
            email=user.email,
            amount_pesewas=round(plan.price_ghs * 100),
            reference=reference,
            callback_url=f"{settings.public_app_url.rstrip('/')}/settings/plans",
            method=method,
            metadata={"userId": user.id, "planId": plan_id},
        )
    except paystack.PaystackError as error:
        payment.status = "failed"
        payment.provider_data = {"error": str(error)}
        db.commit()
        raise AppError(str(error), status_code=502, code="payment_provider") from error
    return {"reference": reference, "authorization_url": url}


def confirm(db: DB, reference: str, user_id: str | None = None) -> SubscriptionSnapshot:
    """Asks Paystack how the payment went and activates the plan once it is paid in full. Safe
    to call twice (the farmer's return and the webhook): a payment is applied once."""
    payment = db.scalars(select(Payment).where(Payment.reference == reference).with_for_update()).first()
    if payment is None or (user_id and payment.user_id != user_id):
        raise AppError("This payment was not found.", status_code=404)
    if payment.status == "pending":
        try:
            data = paystack.verify(reference)
        except paystack.PaystackError as error:
            db.rollback()
            raise AppError(str(error), status_code=502, code="payment_provider") from error
        expected = round(float(payment.amount) * 100)
        paid = (
            data.get("status") == "success"
            and data.get("currency") == "GHS"
            and int(data.get("amount") or 0) == expected
        )
        if paid:
            _apply_payment(db, payment, data)
        elif data.get("status") in ("failed", "abandoned", "reversed"):
            payment.status = "failed"
            payment.provider_data = paystack.receipt_details(data)
        # "ongoing" or "pending" (a MoMo approval still waiting on the phone): left pending.
    user = payment.user_id
    db.commit()
    return snapshot(db, user)


def _apply_payment(db: DB, payment: Payment, data: dict) -> None:
    now = now_utc()
    details = paystack.receipt_details(data)
    payment.status = "paid"
    payment.paid_at = now
    payment.provider_data = details
    row = db.get(Subscription, payment.user_id) or Subscription(user_id=payment.user_id)
    row.plan_id, row.mode = payment.plan_id, "paystack"
    row.starts_at, row.expires_at = now, next_billing_date(now)
    row.payment = MockPayment(
        reference=payment.reference, plan_id=payment.plan_id, amount_ghs=float(payment.amount),
        method=payment.method, created_at=now, status="paid", channel=details.get("channel"),
        brand=details.get("brand"),
    ).model_dump(mode="json", by_alias=True)
    db.merge(row)


def reserve(db: DB, user_id: str, feature: str) -> CreditReservation:
    """Holds the credits one use costs. Committed at once, so two requests can't both spend
    the last credit; settle() keeps or releases the hold."""
    now = now_utc()
    snap = assert_feature(db, user_id, feature, now)
    cost = AI_COSTS[feature]
    if snap.credits_remaining is not None and snap.credits_remaining < cost:
        kind = "scan" if feature == "cropDoctor" else "question"
        raise Forbidden(
            f"Not enough AI credits for this {kind}. Credits reset next month, or choose Advanced.",
            code="credits_exhausted",
        )
    hold = CreditReservation(user_id=user_id, month=month_key(now), feature=feature, cost=cost, created_at=now)
    db.add(hold)
    db.commit()
    return hold


def settle(db: DB, hold: CreditReservation, success: bool) -> SubscriptionSnapshot:
    hold.status = "settled" if success else "released"
    hold.settled_at = now_utc()
    db.commit()
    return snapshot(db, hold.user_id)
