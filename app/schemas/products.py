"""Loan products, their terms and requirements (types/products.ts).

Money and rates are exact decimals, sent as strings ("5000.00"), never floats. Anything the
lender did not state is null and shown as "Not provided"."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, PlainSerializer

from app.domain.constants import REGIONS
from app.schemas.base import CamelModel, Text, Timestamp

DecimalText = PlainSerializer(lambda v: format(v, "f"), return_type=str, when_used="json-unless-none")
Money = Annotated[Decimal, Field(ge=0, le=100_000_000, max_digits=14, decimal_places=2), DecimalText]
Rate = Annotated[Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4), DecimalText]
Region = Literal[tuple(REGIONS)]  # type: ignore[valid-type]
Note = Annotated[str | None, Field(max_length=500)]

Category = Literal["identity", "agricultural", "financial", "other"]
FieldType = Literal["none", "yes_no", "text", "number", "amount", "date", "select", "document"]
Verification = Literal["self_declared", "document_review", "agrictec_verified", "lender_verifies"]
Metric = Literal[
    "ghana_card_verified", "phone_verified", "location_verified", "profile_complete",
    "cooperative_member", "farm_size_acres", "farm_region", "primary_crop", "farm_type",
    "months_of_records", "record_count", "income_12m", "net_12m",
]
ProductStatus = Literal["draft", "published", "unpublished"]


class Fee(CamelModel):
    name: Text(80)
    kind: Literal["fixed", "percent"]
    value: Annotated[Decimal, Field(ge=0, le=100_000_000, max_digits=14, decimal_places=4), DecimalText]
    note: Note = None


class ProductTerms(CamelModel):
    purpose: Annotated[str | None, Field(max_length=300)] = None
    description: Annotated[str | None, Field(max_length=2000)] = None
    min_amount: Money | None = None
    max_amount: Money | None = None
    interest_rate: Rate | None = None
    rate_period: Literal["year", "month"] | None = None
    rate_method: Literal["reducing_balance", "flat", "other"] | None = None
    rate_note: Note = None
    fees: Annotated[list[Fee], Field(max_length=10)] = []
    min_months: Annotated[int | None, Field(ge=1, le=360)] = None
    max_months: Annotated[int | None, Field(ge=1, le=360)] = None
    repayment_frequency: (
        Literal["weekly", "monthly", "quarterly", "end_of_season", "lump_sum", "other"] | None
    ) = None
    collateral: Note = None
    guarantor: Note = None
    # None: the lender didn't say. A list: only these regions.
    supported_regions: Annotated[list[Region], Field(max_length=16)] | None = None


class RequirementCheck(CamelModel):
    """A condition on what AgriCtec knows about the farmer (app/domain/loan_products.METRICS)."""

    metric: Metric
    op: Literal["is_true", "gte", "lte", "in"]
    value: Decimal | list[str] | bool | None = None


class VisibleIf(CamelModel):
    """Show this requirement only when an earlier yes/no or choice requirement has this answer."""

    key: str
    equals: Text(120)


class Requirement(CamelModel):
    key: Annotated[str, Field(pattern=r"^[a-z0-9_]{2,40}$")]
    name: Text(120)
    description: Annotated[str | None, Field(max_length=600)] = None
    category: Category
    required: bool = True
    # What the farmer provides: "none" when only the check applies.
    field_type: FieldType = "none"
    min: Annotated[Decimal | None, DecimalText] = None
    max: Annotated[Decimal | None, DecimalText] = None
    options: Annotated[list[Annotated[str, Field(min_length=1, max_length=120)]], Field(max_length=20)] = []
    allowed_formats: list[Literal["pdf", "jpg", "png", "webp"]] = []
    max_size_mb: Annotated[int | None, Field(ge=1, le=20)] = None
    check: RequirementCheck | None = None
    visible_if: VisibleIf | None = None
    verification: Verification = "self_declared"
    # Where this requirement is stated, when not the product's main source.
    source: Note = None


class ProductContent(CamelModel):
    """One version of a product: its name, terms, requirements and where they come from."""

    name: Text(120)
    terms: ProductTerms = ProductTerms()
    requirements: Annotated[list[Requirement], Field(max_length=40)] = []
    source: Annotated[str | None, Field(max_length=1000)] = None
    source_checked_on: date | None = None


class NewProduct(CamelModel):
    name: Text(120)


class PublishProduct(CamelModel):
    # What changed, for the version history (e.g. "Rate changed from 24% to 22%").
    note: Text(300, min_length=3)


class ProductVersion(ProductContent):
    version: int
    published_at: Timestamp
    published_by: str
    note: str


class LenderSummary(CamelModel):
    id: str
    name: str
    color: str
    logo_path: str | None = None
    partnership_status: str


class LoanProduct(ProductVersion):
    """A published product as farmers see it: the latest published version."""

    id: str
    lender: LenderSummary


class AdminProduct(CamelModel):
    id: str
    lender_id: str
    status: ProductStatus
    # The version farmers see (None: never published).
    published: ProductVersion | None = None
    # What the admin is editing; becomes the next version when published.
    draft: ProductContent
    draft_changed: bool
    # What must be fixed before the draft can be published.
    problems: list[str]
    listed: bool
    created_at: Timestamp
    created_by: str | None = None
    updated_at: Timestamp | None = None
    updated_by: str | None = None
