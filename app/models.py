"""The database tables.

Every per-farmer table cascades from `users`, so deleting a farmer removes all of their data in
one statement. Loan applications are the exception: they keep `user_id` without a foreign key and
stay on record as "Farmer deleted" (lenders keep their history). The audit log is append-only,
enforced by a trigger (see the initial migration).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.domain.clock import now_utc


def new_id() -> str:
    return str(uuid.uuid4())


Money = Numeric(14, 2)
Timestamp = DateTime(timezone=True)
# Python None is stored as SQL NULL (not the JSON value null).
NullableJSON = JSONB(none_as_null=True)


def created_column() -> Mapped[datetime]:
    return mapped_column(Timestamp, default=now_utc, nullable=False)


# ---------- Farmers ----------


class User(Base):
    """A farmer account (the frontend's StoredUser). Admins live in `admins`."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    # None for guests, and for accounts restored from a backup until an admin resets them.
    password_hash: Mapped[str | None] = mapped_column(Text)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    # An admin reset the password: the farmer must choose a new one at the next sign-in.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = created_column()
    last_active_at: Mapped[datetime | None] = mapped_column(Timestamp)

    profile: Mapped["Profile | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("uq_users_email", "email", unique=True, postgresql_where=text("NOT is_guest")),
    )


class Profile(Base):
    __tablename__ = "profiles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    photo_data_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    farm_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    use_name_as_farm_name: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    region: Mapped[str] = mapped_column(String(40), default="", nullable=False, index=True)
    constituency: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    community: Mapped[str | None] = mapped_column(String(80))
    gps_lat: Mapped[float | None] = mapped_column(Float)
    gps_lng: Mapped[float | None] = mapped_column(Float)
    # Always acres; size_unit is how the farmer entered it.
    acres: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    size_unit: Mapped[str] = mapped_column(String(10), default="acres", nullable=False)
    primary_crops: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    other_crops: Mapped[str | None] = mapped_column(String(120))
    farm_type: Mapped[str] = mapped_column(String(20), default="Smallholder", nullable=False)
    cooperative_name: Mapped[str | None] = mapped_column(String(100))
    outgrower_company: Mapped[str | None] = mapped_column(String(100))
    # Never the full card number: a mask (GHA-******789-0) and a SHA-256 fingerprint only.
    ghana_card_status: Mapped[str] = mapped_column(String(16), default="not_provided", nullable=False)
    ghana_card_masked: Mapped[str | None] = mapped_column(String(20))
    ghana_card_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    ghana_card_submitted_at: Mapped[datetime | None] = mapped_column(Timestamp)
    ghana_card_rejected_reason: Mapped[str | None] = mapped_column(Text)
    verified_phone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_location: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(Timestamp)

    user: Mapped[User] = relationship(back_populates="profile")


class FarmRecord(Base):
    __tablename__ = "farm_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    crop: Mapped[str] = mapped_column(String(40), nullable=False)
    amount: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (Index("ix_farm_records_user_date", "user_id", "date"),)


class MomoTransaction(Base):
    __tablename__ = "momo_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    counterparty: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[float] = mapped_column(Money, nullable=False)
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    network: Mapped[str | None] = mapped_column(String(16))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (
        Index("ix_momo_user_date", "user_id", "date"),
        Index("ix_momo_reference_upper", text("upper(reference)")),
    )


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    disease: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    treatment: Mapped[str] = mapped_column(Text, nullable=False)
    prevention: Mapped[list[str] | None] = mapped_column(JSONB)
    crop: Mapped[str | None] = mapped_column(String(40))
    image_thumb: Mapped[str | None] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    created_at: Mapped[datetime] = created_column()


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    # IDs come from the client, so they are unique per farmer only.
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(4), nullable=False)
    # A string (the farmer's message) or a structured AssistantReply.
    content: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(4), default="en", nullable=False)
    notifications: Mapped[dict[str, bool]] = mapped_column(JSONB, default=dict, nullable=False)


class OnboardingDraft(Base):
    __tablename__ = "onboarding_drafts"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = created_column()


class AdminNote(Base):
    """An admin's note on a farmer (deleted with the farmer)."""

    __tablename__ = "admin_notes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    by: Mapped[str] = mapped_column(String(80), nullable=False)
    at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)


class DismissedAnnouncement(Base):
    __tablename__ = "dismissed_announcements"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    announcement_id: Mapped[str] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), primary_key=True
    )
    at: Mapped[datetime] = created_column()


class Subscription(Base):
    """DEMO payments only ("mock" mode): a real gateway must verify payments server-side."""

    __tablename__ = "subscriptions"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    plan_id: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    mode: Mapped[str] = mapped_column(String(8), default="mock", nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(Timestamp)
    expires_at: Mapped[datetime | None] = mapped_column(Timestamp)
    payment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Payment(Base):
    """A subscription payment through Paystack (MTN MoMo, Telecel Cash, AirtelTigo Money, cards).
    A plan is activated only once Paystack confirms the exact amount was paid."""

    __tablename__ = "payments"

    reference: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[float] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="GHS", nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), default="paystack", nullable=False)
    # pending -> paid | failed. Never back once paid.
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    created_at: Mapped[datetime] = created_column()
    paid_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # What the provider reported (channel, card brand, mobile network...). No card numbers.
    provider_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Notification(Base):
    """A message to a farmer: always in the app's inbox, and by SMS or email when configured."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Where in the app it leads ("/loans", "/settings/verification"...).
    href: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = created_column()
    read_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # Delivery outside the app: None (not sent), "sent", "failed: <reason>".
    sms_status: Mapped[str | None] = mapped_column(String(200))
    email_status: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (Index("ix_notifications_user_created", "user_id", "created_at"),)


class CreditReservation(Base):
    """AI credits held for one assistant question or crop scan.

    A month's usage is every settled-successful hold plus the pending ones younger than ten
    minutes, so an abandoned request gives its credits back by itself."""

    __tablename__ = "credit_reservations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    feature: Mapped[str] = mapped_column(String(20), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    created_at: Mapped[datetime] = created_column()
    settled_at: Mapped[datetime | None] = mapped_column(Timestamp)

    __table_args__ = (Index("ix_credit_reservations_user_month", "user_id", "month"),)


# ---------- Admins and sessions ----------


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = created_column()
    created_by: Mapped[str | None] = mapped_column(String(64))
    # A fingerprint of the ADMIN_EMAIL/ADMIN_PASSWORD it was set from, so it is applied once.
    configured_from: Mapped[str | None] = mapped_column(String(64))


class AuthSession(Base):
    """A signed-in browser. Only a SHA-256 of the cookie's token is stored.

    kind: 'farmer', 'admin', or 'impersonation' (an admin viewing the app as a farmer,
    read-only: both admin_id and user_id are set)."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    admin_id: Mapped[str | None] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = created_column()
    last_activity_at: Mapped[datetime] = created_column()
    expires_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)

    __table_args__ = (
        Index("ix_auth_sessions_user", "user_id"),
        Index("ix_auth_sessions_admin", "admin_id"),
    )


class ImpersonationToken(Base):
    """A single-use, one-minute token that opens a read-only farmer view."""

    __tablename__ = "impersonation_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[str] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = created_column()
    expires_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(Timestamp)


class LoginLockout(Base):
    """Failed admin sign-ins per email: the fifth in a row locks it for five minutes."""

    __tablename__ = "login_lockouts"

    key: Mapped[str] = mapped_column(String(254), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(Timestamp)


class RateLimit(Base):
    """Requests counted per client and route, in fixed windows (app/core/ratelimit.py)."""

    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ---------- Platform configuration ----------


class CreditConfigVersion(Base):
    """Every published version of the credit rules. The live one is the highest version;
    nothing is ever rewritten (restoring an old version adds a new one)."""

    __tablename__ = "credit_config_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    weights: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    targets: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    bands: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    eligibility: Mapped[dict[str, float]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = created_column()
    updated_by: Mapped[str] = mapped_column(String(80), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    restored_from: Mapped[int | None] = mapped_column(Integer)


class PlatformSetting(Base):
    """Feature flags, plans and the editable content, each stored as one validated document.

    updated_by is None while a value is still the built-in default."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(NullableJSON, nullable=True)
    updated_at: Mapped[datetime] = created_column()
    updated_by: Mapped[str | None] = mapped_column(String(80))


class Lender(Base):
    __tablename__ = "lenders"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    max_amount: Mapped[float] = mapped_column(Money, nullable=False)
    min_months: Mapped[int] = mapped_column(Integer, nullable=False)
    max_months: Mapped[int] = mapped_column(Integer, nullable=False)
    min_score: Mapped[int] = mapped_column(Integer, nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = created_column()


class LoanApplication(Base):
    __tablename__ = "loan_applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    reference: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    # No foreign key on purpose: applications outlive a deleted farmer account.
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    partner_id: Mapped[str] = mapped_column(
        ForeignKey("lenders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[float] = mapped_column(Money, nullable=False)
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(80), nullable=False)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    monthly: Mapped[float] = mapped_column(Money, nullable=False)
    score_at_application: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    disbursement_date: Mapped[date | None] = mapped_column(Date)
    disbursement_ref: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = created_column()

    history: Mapped[list["LoanStatusEvent"]] = relationship(
        order_by="LoanStatusEvent.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class LoanStatusEvent(Base):
    __tablename__ = "loan_status_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("loan_applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    by: Mapped[str] = mapped_column(String(120), nullable=False)
    at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    disbursed_on: Mapped[date | None] = mapped_column(Date)
    disbursement_ref: Mapped[str | None] = mapped_column(String(40))


class VerificationRequest(Base):
    __tablename__ = "verification_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Masked for Ghana Cards.
    submitted_value: Mapped[str] = mapped_column(String(120), nullable=False)
    value_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    submitted_at: Mapped[datetime] = created_column()
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    reviewed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    reason: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(String(300))
    # An identity provider's answer for a Ghana Card (no card number): advice for the reviewer.
    identity_check: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        # One Ghana Card waiting for review per farmer.
        Index(
            "uq_verification_pending_card",
            "user_id",
            unique=True,
            postgresql_where=text("kind = 'ghanaCard' AND status = 'pending'"),
        ),
    )


class RiskFlag(Base):
    __tablename__ = "risk_flags"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    related_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # rule|user|sorted related IDs: a finding is stored once, whatever its status, so a
    # dismissed flag never comes back on the next scan.
    finding_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False, index=True)
    detected_at: Mapped[datetime] = created_column()
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    reviewed_at: Mapped[datetime | None] = mapped_column(Timestamp)
    note: Mapped[str | None] = mapped_column(Text)


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    audience: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(Timestamp)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    dismissible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = created_column()


class AuditEntry(Base):
    """Append-only: a database trigger refuses UPDATE, DELETE and TRUNCATE for everyone."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_name: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(120), index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[Any] = mapped_column(NullableJSON, nullable=True)
    after: Mapped[Any] = mapped_column(NullableJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=now_utc, nullable=False, index=True)


__all__ = [
    "Admin",
    "AdminNote",
    "Announcement",
    "AuditEntry",
    "AuthSession",
    "ChatMessage",
    "CreditConfigVersion",
    "CreditReservation",
    "Diagnosis",
    "DismissedAnnouncement",
    "FarmRecord",
    "ImpersonationToken",
    "Lender",
    "LoanApplication",
    "LoanStatusEvent",
    "LoginLockout",
    "MomoTransaction",
    "Notification",
    "OnboardingDraft",
    "Payment",
    "PlatformSetting",
    "Profile",
    "RateLimit",
    "RiskFlag",
    "Subscription",
    "User",
    "UserSettings",
    "VerificationRequest",
    "new_id",
]
