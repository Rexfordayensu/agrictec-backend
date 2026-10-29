"""The farmer's own data (types/index.ts), and the loan and verification records both apps use."""

import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.domain.constants import MAX_FARM_SIZE, REGIONS, Crop, inside_ghana
from app.domain.constituencies import is_constituency_of
from app.schemas.base import CamelModel, Text, Timestamp
from app.schemas.platform import AssistantReply, Severity

GHANA_PHONE = re.compile(r"^(\+233|0)[235]\d{8}$")
GHANA_CARD = re.compile(r"^GHA-\d{9}-\d$")
MASKED_CARD = re.compile(r"^GHA-\*{6}\d{3}-\d$")
PHOTO_DATA_URL = re.compile(r"^data:image/jpeg;base64,[A-Za-z0-9+/]+={0,2}$")
MAX_PROFILE_PHOTO_LENGTH = 256 * 1024

GhanaCardStatus = Literal["not_provided", "pending", "verified", "rejected"]
SizeUnit = Literal["acres", "hectares"]
FarmType = Literal["Smallholder", "Commercial", "Cooperative", "Out-grower"]
Language = Literal["en", "tw", "ga", "ee", "ha"]

# ---------- Session ----------


class Session(CamelModel):
    user_id: str
    email: str
    is_guest: bool
    role: Literal["farmer"] = "farmer"
    started_at: Timestamp
    # Set while an admin views the app as this farmer: every write is refused.
    read_only: bool = False


class SignedIn(CamelModel):
    session: Session
    # The same opaque token as the httpOnly cookie, for non-browser clients
    # (send it as "Authorization: Bearer <token>"). Browsers should rely on the cookie.
    token: str


class Credentials(CamelModel):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=200)]


class SignUp(Credentials):
    password: Annotated[str, Field(min_length=8, max_length=200)]


class ReplaceTemporaryPassword(CamelModel):
    email: str
    temporary_password: str
    new_password: Annotated[str, Field(max_length=200)]


# ---------- Profile ----------


class Gps(CamelModel):
    lat: float
    lng: float


class GhanaCardInfo(CamelModel):
    status: GhanaCardStatus = "not_provided"
    masked: str | None = None
    hash: str | None = None
    submitted_at: Timestamp | None = None
    rejected_reason: str | None = None


class VerificationState(CamelModel):
    phone: bool = False
    location: bool = False
    ghana_card: bool = False


class Profile(CamelModel):
    user_id: str
    name: str = ""
    photo_data_url: str | None = None
    email: str = ""
    phone: str = ""
    farm_name: str = ""
    use_name_as_farm_name: bool = False
    region: str = ""
    constituency: str = ""
    community: str | None = None
    gps: Gps | None = None
    acres: float = 0
    size_unit: SizeUnit = "acres"
    primary_crops: list[str] = []
    other_crops: str | None = None
    farm_type: FarmType = "Smallholder"
    cooperative_name: str | None = None
    outgrower_company: str | None = None
    ghana_card: GhanaCardInfo = GhanaCardInfo()
    verification: VerificationState = VerificationState()
    onboarding_completed_at: Timestamp | None = None


class ProfileFields(CamelModel):
    """What a farmer may edit. Verification and the Ghana Card change only through their own
    flows (verifications and admin review), whatever a request sends."""

    name: Annotated[str, Field(max_length=120)] = ""
    photo_data_url: str | None = None
    email: Annotated[str, Field(max_length=254)] = ""
    phone: Annotated[str, Field(max_length=20)] = ""
    farm_name: Annotated[str, Field(max_length=80)] = ""
    use_name_as_farm_name: bool = False
    region: str = ""
    constituency: str = ""
    community: Annotated[str | None, Field(max_length=80)] = None
    gps: Gps | None = None
    acres: Annotated[float, Field(ge=0, le=MAX_FARM_SIZE * 2.48)] = 0
    size_unit: SizeUnit = "acres"
    primary_crops: list[Crop] = []
    other_crops: Annotated[str | None, Field(max_length=120)] = None
    farm_type: FarmType = "Smallholder"
    cooperative_name: Annotated[str | None, Field(max_length=100)] = None
    outgrower_company: Annotated[str | None, Field(max_length=100)] = None

    @field_validator("photo_data_url")
    @classmethod
    def check_photo(cls, value: str | None) -> str | None:
        if value and (len(value) > MAX_PROFILE_PHOTO_LENGTH or not PHOTO_DATA_URL.match(value)):
            raise ValueError("The profile photo must be a JPEG of at most 256 KB.")
        return value or None

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str) -> str:
        value = value.strip()
        if value and not GHANA_PHONE.match(value):
            raise ValueError("Enter a valid Ghana phone number.")
        return value

    @model_validator(mode="after")
    def check(self) -> "ProfileFields":
        if self.region and self.region not in REGIONS:
            raise ValueError("Choose your region.")
        if self.region and self.constituency and not is_constituency_of(self.region, self.constituency):
            raise ValueError("Choose a constituency in this region.")
        if self.gps and not inside_ghana(self.gps.lat, self.gps.lng):
            raise ValueError("This location is outside Ghana.")
        return self


class OnboardingProfile(ProfileFields):
    """The whole profile, checked once more when onboarding completes."""

    farm_name: Annotated[str, Field(min_length=2, max_length=80)]
    region: str
    constituency: Annotated[str, Field(min_length=1)]
    acres: Annotated[float, Field(gt=0, le=MAX_FARM_SIZE * 2.48)]
    primary_crops: Annotated[list[Crop], Field(min_length=1)]

    @model_validator(mode="after")
    def check_complete(self) -> "OnboardingProfile":
        if self.region not in REGIONS:
            raise ValueError("Choose your region.")
        if "Other" in self.primary_crops and len((self.other_crops or "").strip()) < 2:
            raise ValueError("Type the name of your other crop(s).")
        if self.farm_type == "Cooperative" and len((self.cooperative_name or "").strip()) < 2:
            raise ValueError("Enter the name of your cooperative.")
        return self


class OnboardingCardChoice(CamelModel):
    """Step 6. Either the full number (hashed and masked here, never stored) or, from a draft
    saved by the browser, the mask and fingerprint it already computed."""

    card_number: str | None = None
    masked: str | None = None
    hash: Annotated[str | None, Field(pattern=r"^[\w-]{8,128}$")] = None


class OnboardingComplete(CamelModel):
    profile: OnboardingProfile
    ghana_card: OnboardingCardChoice | None = None


class OnboardingDraftInput(CamelModel):
    current_step: Annotated[int, Field(ge=0, le=6)]
    data: dict[str, Any]


class OnboardingDraft(OnboardingDraftInput):
    user_id: str
    updated_at: Timestamp


# ---------- Records, MoMo, diagnoses ----------

RecordType = Literal["income", "expense", "activity"]
MomoNetwork = Literal["MTN", "Telecel", "AirtelTigo"]
Amount = Annotated[float, Field(ge=0, le=1_000_000_000)]


class RecordInput(CamelModel):
    type: RecordType
    description: Text(300)
    crop: Crop
    amount: Amount = 0
    date: dt.date
    notes: Annotated[str | None, Field(max_length=1000)] = None


class RecordPatch(CamelModel):
    type: RecordType | None = None
    description: Text(300) | None = None
    crop: Crop | None = None
    amount: Amount | None = None
    date: dt.date | None = None
    notes: Annotated[str | None, Field(max_length=1000)] = None


class FarmRecord(RecordInput):
    id: str
    user_id: str
    created_at: Timestamp


class MomoInput(CamelModel):
    direction: Literal["in", "out"]
    counterparty: Text(120)
    amount: Annotated[float, Field(gt=0, le=1_000_000_000)]
    # Left blank, a "MOMO######" reference is generated.
    reference: Annotated[str, Field(max_length=64)] = ""
    network: MomoNetwork | None = None
    date: dt.date


class MomoPatch(CamelModel):
    direction: Literal["in", "out"] | None = None
    counterparty: Text(120) | None = None
    amount: Annotated[float | None, Field(gt=0, le=1_000_000_000)] = None
    reference: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    network: MomoNetwork | None = None
    date: dt.date | None = None


class MomoTransaction(MomoInput):
    id: str
    user_id: str
    created_at: Timestamp


class DiagnosisInput(CamelModel):
    disease: Text(80)
    severity: Severity
    confidence: Annotated[float, Field(ge=0, le=100)]
    treatment: Text(1000)
    prevention: list[Text(240)] | None = None
    crop: Crop | None = None
    image_thumb: Annotated[str | None, Field(max_length=200_000)] = None
    date: Timestamp


class Diagnosis(DiagnosisInput):
    id: str
    user_id: str


# ---------- Chat and settings ----------


class ChatMessage(CamelModel):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    role: Literal["user", "ai"]
    content: Annotated[str, Field(max_length=4000)] | AssistantReply
    created_at: Timestamp


class NotificationSettings(CamelModel):
    ai_insights: bool = True
    weather_alerts: bool = True
    price_alerts: bool = True


class AppSettings(CamelModel):
    language: Language = "en"
    notifications: NotificationSettings = NotificationSettings()


# ---------- Credit, loans and verifications ----------


class CreditScoreBreakdown(CamelModel):
    score: int
    payment_history: int
    record_completeness: int
    income_stability: int
    momo_activity: int
    verification: int


class ReadinessItem(CamelModel):
    key: str
    label: str
    description: str
    done: bool


EligibilityIssueCode = Literal["records", "affordability", "score", "lender-max", "ghana-card"]


class EligibilityIssue(CamelModel):
    code: EligibilityIssueCode
    message: str


class EligibilityResult(CamelModel):
    eligible: bool
    reasons: list[str]
    issues: list[EligibilityIssue]
    max_affordable: float


class RepaymentResult(CamelModel):
    monthly: float
    total: float
    interest: float


class EligibilityQuery(CamelModel):
    amount: Annotated[float, Field(ge=0)]
    partner_id: str | None = None
    months: Annotated[int | None, Field(ge=1, le=60)] = None


class CreditSummary(CamelModel):
    """Everything the dashboard and loans page derive from the farmer's data."""

    credit: CreditScoreBreakdown
    band: str
    readiness: list[ReadinessItem]
    annualized_profit: float
    max_affordable: float
    under_review: bool


LoanStatus = Literal[
    "submitted", "under_review", "approved", "rejected", "disbursed", "repaid", "defaulted"
]


class LoanHistoryEntry(CamelModel):
    status: LoanStatus
    by: str
    at: Timestamp
    note: str | None = None
    disbursed_on: dt.date | None = None
    disbursement_ref: str | None = None


class Disbursement(CamelModel):
    date: dt.date
    reference: str


class LoanApplication(CamelModel):
    id: str
    reference: str
    user_id: str
    partner_id: str
    amount: float
    months: int
    purpose: str
    rate: float
    monthly: float
    score_at_application: int
    status: LoanStatus
    history: list[LoanHistoryEntry]
    disbursement: Disbursement | None = None
    created_at: Timestamp


class LoanApplicationInput(CamelModel):
    partner_id: str
    amount: Annotated[float, Field(gt=0, le=10_000_000)]
    purpose: Text(80)
    months: Annotated[int, Field(ge=1, le=60)]


VerificationKind = Literal["phone", "location", "ghanaCard"]


class VerificationRequest(CamelModel):
    id: str
    user_id: str
    kind: VerificationKind
    submitted_value: str
    value_hash: str | None = None
    status: Literal["pending", "approved", "rejected"]
    submitted_at: Timestamp
    reviewed_by: str | None = None
    reviewed_at: Timestamp | None = None
    reason: str | None = None
    note: str | None = None
    # Admins only: an identity provider's answer for a Ghana Card (no card number).
    identity_check: dict[str, Any] | None = None


class VerificationSubmit(CamelModel):
    kind: VerificationKind
    # A phone number, "lat, lng" for a location, or a full Ghana Card number.
    value: Annotated[str, Field(min_length=1, max_length=120)]


# ---------- AI tools ----------


class YieldInput(CamelModel):
    crop: Crop
    acres: Annotated[float, Field(ge=0.1, le=MAX_FARM_SIZE * 2.48)]
    season: Literal["major", "minor"]
    fertilizer: Literal["both", "inorganic", "organic", "none"]
    irrigation: Literal["rainfed", "drip", "sprinkler"]


class YieldResult(CamelModel):
    low: float
    expected: float
    high: float
    per_acre: float
    revenue: float
    price_per_tonne: float
    insight: str
    tips: list[str]


class AssistantQuestion(CamelModel):
    message: Annotated[str, Field(min_length=1, max_length=1000)]


class AssistantAnswer(CamelModel):
    reply: AssistantReply
    credits_remaining: int | None


class DiagnosisResultOut(CamelModel):
    disease: str
    severity: Severity
    confidence: float
    treatment: str
    prevention: list[str] | None = None
    crop: str | None = None


class DiagnosisAnswer(CamelModel):
    result: DiagnosisResultOut
    credits_remaining: int | None


# ---------- Subscriptions ----------


class MockPayment(CamelModel):
    """The receipt of a plan's latest payment: simulated (demo) or paid through Paystack."""

    reference: str
    plan_id: str
    amount_ghs: Annotated[float, Field(alias="amountGHS")]
    method: Literal["mobile-money", "card"]
    created_at: Timestamp
    status: Literal["simulated", "paid"] = "simulated"
    # Paystack only: how it was paid ("mobile_money", "card") and the card or wallet brand.
    channel: str | None = None
    brand: str | None = None


class SubscriptionState(CamelModel):
    user_id: str
    plan_id: str
    mode: Literal["mock", "paystack"] = "mock"
    starts_at: Timestamp | None
    expires_at: Timestamp | None
    payment: MockPayment | None = None


class SubscriptionSnapshot(CamelModel):
    subscription: SubscriptionState
    credits_used: int
    # None means unlimited.
    credits_limit: int | None
    credits_remaining: int | None
    resets_at: Timestamp


class ActivatePlan(CamelModel):
    plan_id: Literal["free", "basic", "premium", "advanced"]
    method: Literal["mobile-money", "card"]


class Checkout(CamelModel):
    """Where to pay: Paystack's checkout page (it sends the farmer back to the plans page)."""

    reference: str
    authorization_url: str


class ConfirmPayment(CamelModel):
    reference: Annotated[str, Field(min_length=4, max_length=64)]


# ---------- The farmer's data export ----------


class DataExport(CamelModel):
    version: Literal[1] = 1
    exported_at: Timestamp
    profile: Profile
    records: list[FarmRecord]
    momo: list[MomoTransaction]
    diagnoses: list[Diagnosis]
    chat: list[ChatMessage]
    settings: AppSettings
    loan_applications: list[LoanApplication]


class DataImport(CamelModel):
    """A file from GET /me/export. The Ghana Card and loan applications are never taken from
    a file; the profile is applied as an ordinary edit."""

    version: Literal[1]
    profile: dict[str, Any]
    records: Annotated[list[RecordInput], Field(max_length=10_000)]
    momo: Annotated[list[MomoInput], Field(max_length=10_000)]
    diagnoses: Annotated[list[DiagnosisInput], Field(max_length=10)] = []
    chat: Annotated[list[ChatMessage], Field(max_length=500)] = []
    settings: AppSettings = AppSettings()

