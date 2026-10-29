"""The admin console's bodies (types/admin.ts)."""

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import Field

from app.schemas.base import CamelModel, Text, Timestamp
from app.schemas.farm import (
    CreditScoreBreakdown,
    Diagnosis,
    FarmRecord,
    MomoTransaction,
    Profile,
)
from app.schemas.platform import CreditConfig

AdminRole = Literal["analyst", "support", "admin", "super_admin"]
AccountStatus = Literal["active", "suspended", "pending_verification"]
TargetType = Literal[
    "farmer", "loan", "lender", "config", "content", "announcement", "risk", "admin", "system"
]

# ---------- Auth and team ----------


class AdminUser(CamelModel):
    id: str
    name: str
    email: str
    role: AdminRole
    status: Literal["active", "disabled"]
    must_change_password: bool
    last_login_at: Timestamp | None = None
    created_at: Timestamp
    created_by: str | None = None


class AdminSession(CamelModel):
    admin: AdminUser
    started_at: Timestamp
    # When the session ends without activity (the console's idle warning counts down to it).
    idle_expires_at: Timestamp
    permissions: list[str]


class AdminSignedIn(CamelModel):
    session: AdminSession
    token: str


class LockStatus(CamelModel):
    locked: bool
    until: Timestamp | None = None
    attempts_left: int


class ChangePassword(CamelModel):
    current_password: str
    new_password: Annotated[str, Field(max_length=200)]


class Rename(CamelModel):
    name: str


class Invite(CamelModel):
    name: str
    email: str
    role: AdminRole


class InviteResult(CamelModel):
    admin: AdminUser
    # Shown once to the inviter; never stored or logged in plain text.
    temporary_password: str


class RoleChange(CamelModel):
    role: AdminRole


class TemporaryPassword(CamelModel):
    temporary_password: str


# ---------- Farmers ----------


class FarmerStats(CamelModel):
    records: int
    momo: int
    income: float
    expenses: float
    score: int
    diagnoses: int


class AdminNote(CamelModel):
    id: str
    text: str
    by: str
    at: Timestamp


class FarmerAccount(CamelModel):
    user_id: str
    email: str
    is_guest: bool
    status: AccountStatus
    suspended_reason: str | None = None
    created_at: Timestamp
    last_active_at: Timestamp | None = None
    profile: Profile
    stats: FarmerStats
    risk_confirmed: int
    tags: list[str]
    admin_notes: list[AdminNote]


class FarmerDetail(CamelModel):
    account: FarmerAccount
    records: list[FarmRecord]
    momo: list[MomoTransaction]
    diagnoses: list[Diagnosis]
    credit: CreditScoreBreakdown
    credit_config: CreditConfig


class Suspend(CamelModel):
    reason: str


class NoteInput(CamelModel):
    text: str


class TagsInput(CamelModel):
    tags: list[str]


class IdList(CamelModel):
    ids: list[str] | None = None


class ImpersonationStart(CamelModel):
    token: str
    url: str
    expires_at: Timestamp


# ---------- Verifications, loans, risk ----------


class Reject(CamelModel):
    reason: str
    note: str = ""


class StatusChange(CamelModel):
    status: Literal[
        "submitted", "under_review", "approved", "rejected", "disbursed", "repaid", "defaulted"
    ]
    note: Annotated[str | None, Field(max_length=500)] = None
    # "disbursed" only: the day the funds went out and the transfer reference.
    disbursed_on: str | None = None
    disbursement_ref: Annotated[str | None, Field(max_length=40)] = None


class EligibilityCheck(CamelModel):
    key: Literal["records", "affordability", "score", "lender-max", "term"]
    label: str
    pass_: bool = Field(alias="pass")
    detail: str


class LoanReview(CamelModel):
    """An application's eligibility re-run with today's rules and the farmer's current data."""

    farmer_exists: bool
    current_score: int | None
    checks: list[EligibilityCheck]


class RiskFlag(CamelModel):
    id: str
    user_id: str
    rule: str
    severity: Literal["low", "medium", "high"]
    description: str
    related_ids: list[str]
    status: Literal["open", "dismissed", "confirmed"]
    detected_at: Timestamp
    reviewed_by: str | None = None
    reviewed_at: Timestamp | None = None
    note: str | None = None


class RiskReview(CamelModel):
    note: str
    suspend: bool = False


class RelatedMomo(MomoTransaction):
    owner_name: str
    related: bool


class RelatedRecord(FarmRecord):
    related: bool


class RiskRelated(CamelModel):
    kind: Literal["momo", "records", "accounts"]
    farmer: FarmerAccount
    accounts: list[FarmerAccount]
    momo: list[RelatedMomo]
    records: list[RelatedRecord]


class RiskRuleInfo(CamelModel):
    id: str
    label: str
    explanation: str
    guidance: str
    severity: str
    related: str


# ---------- Audit ----------


class AuditEntry(CamelModel):
    id: str
    actor_id: str
    actor_name: str
    actor_role: AdminRole
    action: str
    target_type: TargetType
    target_id: str | None = None
    summary: str
    before: Any = None
    after: Any = None
    created_at: Timestamp


class AuditActor(CamelModel):
    id: str
    name: str
    role: AdminRole


class AuditFilter(CamelModel):
    actor_id: str | None = None
    # Exact action, or a prefix ending in "." such as "farmer.".
    action: str | None = None
    group: Literal["farmer", "loan", "config", "content", "team", "system", "auth"] | None = None
    target_type: TargetType | None = None
    target_id: str | None = None
    # Inclusive ISO date or timestamp bounds.
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    search: str | None = None


class AuditExport(CamelModel):
    filter: AuditFilter = AuditFilter()
    format: Literal["json", "csv"] = "json"


class FileExport(CamelModel):
    filename: str
    content: str


# ---------- Credit rules preview ----------


class ImpactRequest(CamelModel):
    draft: dict[str, Any]


class FarmerImpact(CamelModel):
    user_id: str
    name: str
    region: str
    current: int
    draft: int
    delta: int
    current_band: str
    draft_band: str
    eligible_now: bool
    eligible_draft: bool


class Impact(CamelModel):
    farmers: list[FarmerImpact]
    average_current: int
    average_draft: int
    bands_current: dict[str, int]
    bands_draft: dict[str, int]
    gained: list[FarmerImpact]
    lost: list[FarmerImpact]
    biggest: list[FarmerImpact]
    changed_scores: int


# ---------- Reports ----------


class ReportTable(CamelModel):
    title: str
    headers: list[str]
    rows: list[list[str | int | float]]
    numeric: list[int]


class ReportSummaryItem(CamelModel):
    label: str
    value: str


class AdminReport(CamelModel):
    id: str
    title: str
    description: str
    generated_at: Timestamp
    summary: list[ReportSummaryItem]
    tables: list[ReportTable]


class BuilderSpec(CamelModel):
    dataset: Literal["farmers", "records", "momo", "applications"]
    columns: list[str]
    region: str | None = None
    crop: str | None = None
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None


class BuilderResult(CamelModel):
    headers: list[str]
    rows: list[list[str | int | float]]
    total: int


# ---------- System ----------


class BackupImport(CamelModel):
    backup: dict[str, Any]
    confirmation: str


class DeleteAllFarmers(CamelModel):
    confirmation: str
    password: str


class ResetDemo(CamelModel):
    confirmation: str


class DeleteFarmer(CamelModel):
    """The farmer's email or name, typed exactly."""

    confirmation: Text(254)


class AssistantTest(CamelModel):
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    farmer_id: str | None = None


class AssistantTestResult(CamelModel):
    item_id: str | None
    matched_keyword: str | None
    reply: dict[str, Any]

