"""The database tables.

Every per-farmer table cascades from `users`, so deleting a farmer removes all of their data in
one statement. Loan applications are the exception: they keep `user_id` without a foreign key and
stay on record as "Farmer deleted" (lenders keep their history). The audit log is append-only,
enforced by a trigger (see the initial migration).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    UniqueConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
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


class StoredFile(Base):
    """Private uploads, included in database and encrypted platform backups."""

    __tablename__ = "stored_files"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)


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


class TransactionFields:
    category: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3), default="GHS", server_default="GHS", nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), default="farmer_entry", server_default="farmer_entry", nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), default="self_reported", server_default="self_reported", nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(120))
    payment_method: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime | None] = mapped_column(Timestamp)


def transaction_checks(table: str):
    statuses = "'self_reported','evidence_submitted','verification_pending','verified','rejected','disputed'"
    return (
        CheckConstraint("source_type IN ('farmer_entry','statement_upload')", name=f"ck_{table}_source"),
        CheckConstraint(f"verification_status IN ({statuses})", name=f"ck_{table}_status"),
        CheckConstraint("currency = 'GHS'", name=f"ck_{table}_currency"),
    )


class FarmRecord(TransactionFields, Base):
    __tablename__ = "farm_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    crop: Mapped[str] = mapped_column(String(40), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, default=0, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(String(3))
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (
        Index("ix_farm_records_user_date", "user_id", "date"),
        Index("ix_farm_records_user_status", "user_id", "verification_status"),
        *transaction_checks("farm_records"),
        CheckConstraint("type IN ('income','expense','activity','financing')", name="ck_farm_records_type"),
        CheckConstraint("category IS NULL OR (type='income' AND category IN ('crop_sale','livestock_sale','produce_delivery','other_farm_income')) OR (type='expense' AND category IN ('fertilizer','seed','equipment','transport','labour','irrigation','land_rent','other_farm_expense')) OR (type='financing' AND category IN ('loan_disbursement','loan_repayment','interest_payment','other_financing'))", name="ck_farm_records_category"),
        CheckConstraint("(type='financing' AND direction IS NOT NULL AND direction IN ('in','out')) OR (type<>'financing' AND direction IS NULL)", name="ck_farm_records_financing_direction"),
    )


class MomoTransaction(TransactionFields, Base):
    __tablename__ = "momo_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    counterparty: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    reference_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    network: Mapped[str | None] = mapped_column(String(16))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (
        Index("ix_momo_user_date", "user_id", "date"),
        Index("ix_momo_user_status", "user_id", "verification_status"),
        *transaction_checks("momo_transactions"),
        CheckConstraint("category IS NULL OR category IN ('crop_sale','livestock_sale','produce_delivery','other_farm_income','fertilizer','seed','equipment','transport','labour','irrigation','land_rent','other_farm_expense','loan_disbursement','loan_repayment','interest_payment','other_financing')", name="ck_momo_transactions_category"),
        Index("ux_momo_user_reference", "user_id", text("upper(reference)"), unique=True,
              postgresql_where=text("reference IS NOT NULL AND reference <> '' AND NOT reference_duplicate")),
        Index("ix_momo_reference_upper", text("upper(reference)")),
    )


class TransactionEvidence(Base):
    __tablename__ = "transaction_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    file_key: Mapped[str] = mapped_column(ForeignKey("stored_files.key"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    uploaded_at: Mapped[datetime] = created_column()
    __table_args__ = (UniqueConstraint("user_id", "sha256", name="uq_evidence_user_hash"),
                      CheckConstraint("kind IN ('receipt','statement','other')", name="ck_evidence_kind"))


class TransactionEvidenceLink(Base):
    __tablename__ = "transaction_evidence_links"
    evidence_id: Mapped[str] = mapped_column(ForeignKey("transaction_evidence.id", ondelete="CASCADE"), primary_key=True)
    record_kind: Mapped[str] = mapped_column(String(4), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    __table_args__ = (CheckConstraint("record_kind IN ('farm','momo')", name="ck_evidence_link_kind"),
                      Index("ix_evidence_record", "record_kind", "record_id"))


class TransactionVerification(Base):
    __tablename__ = "transaction_verifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    record_kind: Mapped[str] = mapped_column(String(4), nullable=False)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(8), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = created_column()
    __table_args__ = (Index("ix_transaction_history_record", "record_kind", "record_id", "at"),)


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
    source: Mapped[str | None] = mapped_column(String(8))
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


class PasswordReset(Base):
    """A one-time code a farmer asked for to choose a new password. Only a keyed hash of the
    code is kept; it expires, allows a few attempts, and a newer request replaces it."""

    __tablename__ = "password_resets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_column()
    expires_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set when the code is used, or replaced by a newer one.
    used_at: Mapped[datetime | None] = mapped_column(Timestamp)

    __table_args__ = (Index("ix_password_resets_user_created", "user_id", "created_at"),)


class FarmAlert(Base):
    """A weather or market price alert already sent to a farmer, so the same alert (the same
    day's heavy rain, the same price change) never goes out twice."""

    __tablename__ = "farm_alerts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # e.g. "weather:wet:2026-11-12", "weather:dry:2026-W46", "price:<observation id>:<price>".
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (Index("ux_farm_alerts_user_key", "user_id", "key", unique=True),)


class PhoneCode(Base):
    """A one-time code sent by SMS to prove a farmer owns their phone number. Only a keyed hash
    of the code is kept; it is bound to the number it was sent to, expires, allows a few
    attempts, and a newer request replaces it."""

    __tablename__ = "phone_codes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_column()
    expires_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set when the code is used, or replaced by a newer one.
    used_at: Mapped[datetime | None] = mapped_column(Timestamp)

    __table_args__ = (Index("ix_phone_codes_user_created", "user_id", "created_at"),)


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


class ReportSchedule(Base):
    """A console report emailed every week or month, with its CSV attached."""

    __tablename__ = "report_schedules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    report_id: Mapped[str] = mapped_column(String(40), nullable=False)
    # "weekly" (Mondays) or "monthly" (the 1st), at 07:00 Ghana time.
    frequency: Mapped[str] = mapped_column(String(10), nullable=False)
    recipients: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("admins.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_column()
    next_run_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # "sent to 3", or "failed: <reason>".
    last_status: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (Index("ix_report_schedules_due", "active", "next_run_at"),)


class PushSubscription(Base):
    """A phone or computer where an admin turned on error alerts (web push, app/core/alerts.py).
    Never in a platform backup."""

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    admin_id: Mapped[str] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The browser's push service address for this device, and its encryption keys.
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(200), nullable=False)
    auth: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = created_column()


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
    """A real financial institution, added and checked by an administrator. Farmers see it only
    when app/domain/lenders.py says so (published, licence verified recently, listing
    authorised). Loan terms belong to its loan products, not to the institution."""

    __tablename__ = "lenders"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    # Institution
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    institution_type: Mapped[str] = mapped_column(String(32), nullable=False)
    registered_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    logo_key: Mapped[str | None] = mapped_column(String(200))
    logo_updated_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # Contact (official channels only)
    website: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(40))
    # Agricultural lending, as the institution states it (None: not known yet)
    offers_agri_loans: Mapped[bool | None] = mapped_column(Boolean)
    supported_regions: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    # Regulation: checked against an official source
    licence_ref: Mapped[str | None] = mapped_column(String(120))
    regulator: Mapped[str | None] = mapped_column(String(120))
    licence_status: Mapped[str] = mapped_column(String(16), default="unverified", nullable=False)
    verification_source: Mapped[str | None] = mapped_column(Text)
    verification_note: Mapped[str | None] = mapped_column(String(500))
    verified_at: Mapped[datetime | None] = mapped_column(Timestamp)
    verified_by: Mapped[str | None] = mapped_column(String(80))
    # Listing and partnership: separate from regulation
    listing_authorised: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    listing_note: Mapped[str | None] = mapped_column(String(500))
    partnership_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    publication_status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False, index=True)
    suspension_reason: Mapped[str | None] = mapped_column(String(300))
    published_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime | None] = mapped_column(Timestamp)
    updated_by: Mapped[str | None] = mapped_column(String(80))


class FinancialReportExport(Base):
    """A financial report a farmer generated: the PDF (in stored_files) and the exact figures it
    was made from. Never changed afterwards; the farmer can delete it."""

    __tablename__ = "financial_report_exports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    period_months: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    file_key: Mapped[str] = mapped_column(ForeignKey("stored_files.key"), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class LoanProduct(Base):
    """A loan a lender offers, as an administrator typed it from the lender's official terms.
    `draft` is being edited; each publish freezes it into a new LoanProductVersion."""

    __tablename__ = "loan_products"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    lender_id: Mapped[str] = mapped_column(
        ForeignKey("lenders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The version farmers see (None: never published).
    published_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_column()
    created_by: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime | None] = mapped_column(Timestamp)
    updated_by: Mapped[str | None] = mapped_column(String(80))


class LoanProductVersion(Base):
    """A published version: never changed afterwards (a database trigger refuses updates), so
    an application can always show the exact terms and requirements it was prepared against."""

    __tablename__ = "loan_product_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("loan_products.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    min_months: Mapped[int | None] = mapped_column(Integer)
    max_months: Mapped[int | None] = mapped_column(Integer)
    # Every term (amounts and rate included, as text), exactly as published.
    terms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_checked_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str] = mapped_column(String(300), nullable=False)
    published_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    published_by: Mapped[str] = mapped_column(String(80), nullable=False)

    requirements: Mapped[list["LoanRequirement"]] = relationship(
        order_by="LoanRequirement.position", lazy="selectin", passive_deletes=True
    )

    __table_args__ = (
        Index("ux_loan_product_versions", "product_id", "version", unique=True),
    )


class LoanRequirement(Base):
    """One requirement of a published version, in the lender's order."""

    __tablename__ = "loan_requirements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("loan_product_versions.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    field_type: Mapped[str] = mapped_column(String(12), nullable=False)
    verification: Mapped[str] = mapped_column(String(20), nullable=False)
    # Description, limits, options, file types, the check, conditional visibility, source.
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ux_loan_requirements_key", "version_id", "key", unique=True),)


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

    # The lender's name travels with the application (farmers never see unlisted lenders).
    lender: Mapped["Lender"] = relationship(lazy="selectin", viewonly=True)
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


IDENTITY_STATUSES = ("pending", "processing", "verified", "failed", "expired", "requires_review")


class IdentityVerification(Base):
    """One Ghana Card identity verification attempt with an identity provider (NIA's approved
    platform). Only the outcome is kept: never the card number, a photo or biometric data.
    "not_started" is the absence of a row. A typed-in card number reviewed by an admin is a
    different, lower level (profiles.ghana_card_status), never this."""

    __tablename__ = "identity_verifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # "nia", or "simulated" (development only: never counts in production).
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_reference: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # As the provider reports it (e.g. a biometric match), never inferred here.
    verification_method: Mapped[str | None] = mapped_column(String(60))
    consent_version: Mapped[str] = mapped_column(String(40), nullable=False)
    consent_given_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    initiated_at: Mapped[datetime] = mapped_column(Timestamp, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # When an open session lapses (it then becomes "expired").
    expires_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # A sanitised reason from a fixed list (app/services/ghana_card_verification.py).
    failure_code: Mapped[str | None] = mapped_column(String(40))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The last provider event applied, so a replayed callback is ignored.
    last_event_id: Mapped[str | None] = mapped_column(String(160))
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    review_note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = created_column()

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'verified', 'failed', 'expired', 'requires_review')",
            name="ck_identity_verification_status",
        ),
        CheckConstraint("attempt_count >= 1", name="ck_identity_verification_attempts"),
        Index("ix_identity_verifications_user", "user_id", "created_at"),
        Index("ix_identity_verifications_status", "status"),
        # One open session per farmer.
        Index(
            "uq_identity_verification_active",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
        # A provider reference names one attempt.
        Index(
            "uq_identity_verification_reference",
            "provider",
            "verification_reference",
            unique=True,
            postgresql_where=text("verification_reference IS NOT NULL"),
        ),
    )


class LoanEligibilityAssessment(Base):
    """One preliminary eligibility check of a farmer against a published loan product version:
    the request, the outcome and the full explained result as it was shown (kept so a later
    application can carry the assessment it was based on). Never a loan decision."""

    __tablename__ = "loan_eligibility_assessments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(120), ForeignKey("loan_products.id", ondelete="CASCADE"), nullable=False)
    product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    months: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('meets_preliminary', 'additional_information', 'does_not_meet', 'unable_to_assess')",
            name="ck_loan_assessment_outcome",
        ),
        CheckConstraint("amount > 0 AND months > 0", name="ck_loan_assessment_request"),
        Index("ix_loan_assessments_user", "user_id", "created_at"),
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
    "FarmAlert",
    "FarmRecord",
    "FinancialReportExport",
    "IdentityVerification",
    "LoanEligibilityAssessment",
    "ImpersonationToken",
    "Lender",
    "LoanApplication",
    "LoanProduct",
    "LoanProductVersion",
    "LoanRequirement",
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
