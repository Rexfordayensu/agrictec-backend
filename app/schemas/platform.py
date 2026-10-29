"""Platform configuration and content (lib/admin/schemas.ts, types/admin.ts, types/market.ts)."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, model_validator

from app.domain.constants import REGIONS, Crop, YieldCrop
from app.schemas.base import CamelModel, Text, Timestamp

Percent = Annotated[int, Field(ge=0, le=100)]
Score = Annotated[int, Field(ge=300, le=850)]

# ---------- Credit rules ----------


class CreditWeights(CamelModel):
    payment_history: Percent
    record_completeness: Percent
    income_stability: Percent
    momo_activity: Percent
    verification: Percent


class CreditTargets(CamelModel):
    records_for_full: Annotated[int, Field(ge=1, le=1000)]
    momo_for_full: Annotated[int, Field(ge=1, le=1000)]
    months_window: Annotated[int, Field(ge=1, le=24)]


class CreditBands(CamelModel):
    fair: Score
    good: Score
    excellent: Score


class CreditEligibility(CamelModel):
    min_records: Annotated[int, Field(ge=0, le=1000)]
    min_score: Score
    max_profit_multiplier: Annotated[float, Field(ge=0.5, le=20)]


class CreditConfigInput(CamelModel):
    weights: CreditWeights
    targets: CreditTargets
    bands: CreditBands
    eligibility: CreditEligibility

    @model_validator(mode="after")
    def check(self) -> "CreditConfigInput":
        total = sum(self.weights.model_dump().values())
        if total != 100:
            raise ValueError(f"Weights must add up to 100 (they add up to {total}).")
        b = self.bands
        if not (b.fair < b.good < b.excellent):
            raise ValueError("Bands must rise: Fair < Good < Excellent.")
        return self

    def values(self) -> dict:
        """The editable values, as stored and compared."""
        return self.model_dump(by_alias=True, include={"weights", "targets", "bands", "eligibility"})


class CreditConfig(CreditConfigInput):
    version: Annotated[int, Field(ge=1)]
    updated_at: Timestamp
    updated_by: str
    note: Annotated[str | None, Field(max_length=500)] = None
    restored_from: int | None = None


class PublishCreditConfig(CamelModel):
    config: CreditConfigInput
    note: str


class RestoreCreditConfig(CamelModel):
    version: int
    note: str | None = None


# ---------- Feature flags and plans ----------


class FeatureFlags(CamelModel):
    signup_open: bool
    guest_mode: bool
    ai_assistant: bool
    crop_doctor: bool
    yield_predictor: bool
    loans: bool
    market: bool
    pdf_reports: bool
    maintenance_mode: bool
    maintenance_message: Annotated[str, Field(max_length=300)]
    # Maintenance switches itself off at this time.
    maintenance_ends_at: Timestamp | None = None


FeatureKey = Literal["aiAssistant", "cropDoctor", "yieldPredictor", "loans", "market", "pdfReports"]
PlanId = Literal["free", "basic", "premium", "advanced"]
PlanFeature = Literal["pdfReports", "aiAssistant", "cropDoctor", "yieldPredictor"]


class PlanAccess(CamelModel):
    pdf_reports: bool
    ai_assistant: bool
    crop_doctor: bool
    yield_predictor: bool


class Plan(CamelModel):
    id: PlanId
    name: Text(40)
    price_ghs: Annotated[float, Field(ge=0, le=10000, alias="priceGHS")]
    features: Annotated[list[Text(80)], Field(max_length=12)]
    active: bool
    access: PlanAccess | None = None
    # Monthly AI credits; None means unlimited.
    ai_credits: Annotated[int | None, Field(ge=0, le=1_000_000)] = None


class PlanList(CamelModel):
    plans: list[Plan]

    @model_validator(mode="after")
    def check(self) -> "PlanList":
        ids = [p.id for p in self.plans]
        if len(set(ids)) != len(ids):
            raise ValueError("Each plan appears once.")
        if not any(p.id == "free" and p.active and p.price_ghs == 0 for p in self.plans):
            raise ValueError("Free must remain available at GHS 0.")
        for p in self.plans:
            if p.id == "premium" and "ai_credits" in p.model_fields_set and (p.ai_credits or 0) < 1:
                raise ValueError("Premium needs a limited allowance of at least 1 AI credit.")
            if p.id != "free" and p.price_ghs < 1:
                raise ValueError("Paid plans need a price of at least GHS 1.")
        return self


# ---------- Lenders ----------


class LenderFields(CamelModel):
    name: Text(80)
    rate: Annotated[float, Field(ge=1, le=60)]
    max_amount: Annotated[float, Field(ge=500, le=10_000_000)]
    min_months: Annotated[int, Field(ge=1, le=60)]
    max_months: Annotated[int, Field(ge=1, le=60)]
    min_score: Score
    color: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
    active: bool
    description: Annotated[str | None, Field(max_length=200)] = None


class LenderInput(LenderFields):
    @model_validator(mode="after")
    def check(self) -> "LenderInput":
        if self.max_months < self.min_months:
            raise ValueError("Maximum months must be at least the minimum.")
        return self


class LenderPatch(CamelModel):
    name: Text(80) | None = None
    rate: float | None = None
    max_amount: float | None = None
    min_months: int | None = None
    max_months: int | None = None
    min_score: int | None = None
    color: str | None = None
    active: bool | None = None
    description: str | None = None


class Lender(LenderFields):
    id: str
    created_at: Timestamp


# ---------- Market prices (evidence-backed observations) ----------

MarketUnit = Literal["kg", "bag", "crate", "tonne"]


class MarketSource(CamelModel):
    title: Annotated[str, Field(max_length=200)]
    publisher: Annotated[str, Field(max_length=160)]
    url: Annotated[str, Field(max_length=2048)]
    published_at: Annotated[str, Field(max_length=10)]
    kind: Literal["official", "commodity", "market-report"]
    excerpt: Annotated[str | None, Field(max_length=2000)] = None


class MarketPrice(CamelModel):
    country: Literal["GH"] | None = None
    currency: Literal["GHS"] | None = None
    crop: Text(80)
    unit: Text(40)
    price: Annotated[float, Field(ge=0, le=1_000_000)]
    change: Annotated[float, Field(ge=-100, le=1000)]
    id: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    status: Literal["draft", "published"] | None = None
    scope: Literal["market", "region", "national"] | None = None
    region: Annotated[str | None, Field(max_length=80)] = None
    market: Annotated[str | None, Field(max_length=160)] = None
    quantity: Annotated[float | None, Field(gt=0, le=100_000)] = None
    package_weight_kg: Annotated[float | None, Field(gt=0, le=100_000)] = None
    price_max: Annotated[float | None, Field(gt=0, le=1_000_000)] = None
    observed_at: date | None = None
    source: MarketSource | None = None
    reviewed_at: Timestamp | None = None
    reviewed_by: Annotated[str | None, Field(min_length=1, max_length=160)] = None
    national_coverage_confirmed: bool | None = None


class PublishedSource(CamelModel):
    """A source complete enough to publish a price from."""

    title: Text(200)
    publisher: Text(160)
    url: HttpUrl
    published_at: date
    kind: Literal["official", "commodity", "market-report"]


# ---------- Weather, crops, assistant, crop doctor ----------

WeatherCondition = Literal["sunny", "partly-cloudy", "showers", "storm"]


class Weather(CamelModel):
    condition: WeatherCondition
    temp: Annotated[float, Field(ge=-10, le=60)]
    description: Text(80)
    humidity: Percent
    rain_chance: Percent


class CropContent(CamelModel):
    crop: YieldCrop
    emoji: Text(8)
    yield_per_acre: Annotated[float, Field(gt=0, le=1000)]
    price_per_tonne: Annotated[float, Field(gt=0, le=1_000_000)]
    tips: Annotated[list[Text(240)], Field(max_length=10)]


class AssistantReply(CamelModel):
    title: Text(120)
    intro: Annotated[str | None, Field(max_length=600)] = None
    bullets: Annotated[list[Text(400)] | None, Field(max_length=10)] = None
    tip: Annotated[str | None, Field(max_length=400)] = None


class AssistantKnowledgeItem(CamelModel):
    id: Text(80)
    # The fallback reply (no match) has no keywords; every other item needs at least one.
    keywords: Annotated[list[Text(60)], Field(max_length=30)]
    reply: AssistantReply
    active: bool
    priority: Annotated[int, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def check(self) -> "AssistantKnowledgeItem":
        from app.domain.defaults import ASSISTANT_FALLBACK_ID

        self.keywords = [k.lower() for k in self.keywords]
        if self.id != ASSISTANT_FALLBACK_ID and not self.keywords:
            raise ValueError("Add at least one keyword.")
        return self


Severity = Literal["None", "Moderate", "High"]


class DiagnosisResult(CamelModel):
    disease: Text(80)
    severity: Severity
    confidence: Annotated[float, Field(ge=0, le=100)]
    treatment: Text(1000)
    prevention: Annotated[list[Text(240)] | None, Field(max_length=8)] = None
    crop: Crop | None = None


class DiagnosisContent(DiagnosisResult):
    id: Text(80)
    active: bool
    confidence_min: Annotated[int | None, Field(ge=1, le=100)] = None
    confidence_max: Annotated[int | None, Field(ge=1, le=100)] = None

    @model_validator(mode="after")
    def check(self) -> "DiagnosisContent":
        low, high = self.confidence_min, self.confidence_max
        if (low is None) != (high is None) or (low is not None and low > (high or 0)):
            raise ValueError("The confidence range must run from low to high.")
        return self


class ContentMeta(CamelModel):
    by: str
    at: Timestamp


# ---------- Announcements ----------


class AudienceAll(CamelModel):
    kind: Literal["all"]


class AudienceRegion(CamelModel):
    kind: Literal["region"]
    regions: Annotated[list[str], Field(min_length=1)]

    @model_validator(mode="after")
    def check(self) -> "AudienceRegion":
        unknown = [r for r in self.regions if r not in REGIONS]
        if unknown:
            raise ValueError(f"Unknown region: {unknown[0]}.")
        return self


class AudienceCrop(CamelModel):
    kind: Literal["crop"]
    crops: Annotated[list[Crop], Field(min_length=1)]


Audience = Annotated[AudienceAll | AudienceRegion | AudienceCrop, Field(discriminator="kind")]
AnnouncementType = Literal["info", "success", "warning", "critical"]

TITLE_MAX = 80
MESSAGE_MAX = 300


class AnnouncementInput(CamelModel):
    title: Text(TITLE_MAX)
    message: Text(MESSAGE_MAX)
    type: AnnouncementType
    audience: Audience
    starts_at: Timestamp
    ends_at: Timestamp | None = None
    active: bool
    dismissible: bool

    @model_validator(mode="after")
    def check(self) -> "AnnouncementInput":
        if self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("The end must be after the start.")
        return self


class AnnouncementCreate(AnnouncementInput):
    confirmed_critical: bool = False


class AnnouncementPatch(CamelModel):
    title: str | None = None
    message: str | None = None
    type: AnnouncementType | None = None
    audience: Audience | None = None
    starts_at: Timestamp | None = None
    ends_at: Timestamp | None = None
    active: bool | None = None
    dismissible: bool | None = None
    confirmed_critical: bool = False


class Announcement(AnnouncementInput):
    id: str
    created_by: str
    created_at: Timestamp
