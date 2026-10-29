"""Platform settings: credit rules, feature flags, plans, lenders and content. Reads are open
(the farmer app needs them); the admin services own the writes."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from app.domain.clock import now_utc
from app.domain.credit import default_config
from app.domain.defaults import (
    ASSISTANT_FALLBACK_ID,
    DEFAULT_ASSISTANT_KB,
    DEFAULT_CROP_CONTENT,
    DEFAULT_DIAGNOSES,
    DEFAULT_FLAGS,
    DEFAULT_MARKET_PRICES,
    PLAN_CATALOG,
    fresh,
)
from app.domain.plans import normalize_plans
from app.models import CreditConfigVersion, Lender, PlatformSetting
from app.schemas.platform import (
    AssistantKnowledgeItem,
    CreditConfig,
    CropContent,
    DiagnosisContent,
    FeatureFlags,
    MarketPrice,
    Plan,
    Weather,
)
from app.services.mappers import credit_config_out, lender_out

# Keys of the platform_settings table. The content keys keep the frontend's storage names, so
# "who saved it" metadata reads the same in both.
FLAGS = "flags"
PLANS = "plans"
MARKET = "platform:content:market"
WEATHER_OVERRIDE = "platform:content:weather-override"
CROPS = "platform:content:crops"
ASSISTANT_KB = "platform:content:assistant-kb"
DIAGNOSES = "platform:content:diagnoses"
SEEDED = "seeded"
CONTENT_KEYS = [MARKET, WEATHER_OVERRIDE, CROPS, ASSISTANT_KB, DIAGNOSES]

DEFAULTS: dict[str, Any] = {
    FLAGS: DEFAULT_FLAGS,
    PLANS: PLAN_CATALOG,
    MARKET: DEFAULT_MARKET_PRICES,
    WEATHER_OVERRIDE: None,
    CROPS: DEFAULT_CROP_CONTENT,
    ASSISTANT_KB: DEFAULT_ASSISTANT_KB,
    DIAGNOSES: DEFAULT_DIAGNOSES,
}


def get_raw(db: DB, key: str) -> Any:
    row = db.get(PlatformSetting, key)
    return fresh(row.value) if row else fresh(DEFAULTS.get(key))


def put_raw(db: DB, key: str, value: Any, by: str | None) -> None:
    row = db.get(PlatformSetting, key)
    if row is None:
        db.add(PlatformSetting(key=key, value=value, updated_at=now_utc(), updated_by=by))
    else:
        row.value = value
        row.updated_at = now_utc()
        row.updated_by = by


def content_meta(db: DB) -> dict[str, dict]:
    """Who last saved each content type, and when (only saves by an admin count)."""
    rows = db.scalars(
        select(PlatformSetting).where(
            PlatformSetting.key.in_(CONTENT_KEYS), PlatformSetting.updated_by.is_not(None)
        )
    )
    return {row.key: {"by": row.updated_by, "at": row.updated_at} for row in rows}


# ---------- Typed reads ----------


def credit_config(db: DB) -> CreditConfig:
    """The live credit rules: the highest version."""
    row = db.scalars(select(CreditConfigVersion).order_by(CreditConfigVersion.version.desc()).limit(1)).first()
    if row is None:
        return default_config()
    return credit_config_out(row)


def credit_history(db: DB) -> list[CreditConfig]:
    """Every version but the live one, newest first."""
    rows = db.scalars(select(CreditConfigVersion).order_by(CreditConfigVersion.version.desc())).all()
    return [credit_config_out(r) for r in rows[1:]]


def max_credit_version(db: DB) -> int:
    return db.scalar(select(func.max(CreditConfigVersion.version))) or 0


def flags(db: DB) -> FeatureFlags:
    """Maintenance with an end time in the past is reported as off: it ends by itself."""
    value = FeatureFlags.model_validate({**DEFAULT_FLAGS, **(get_raw(db, FLAGS) or {})})
    if value.maintenance_mode and value.maintenance_ends_at and value.maintenance_ends_at <= now_utc():
        return value.model_copy(update={"maintenance_mode": False, "maintenance_ends_at": None})
    return value


def plans(db: DB) -> list[Plan]:
    return [Plan.model_validate(p) for p in normalize_plans(get_raw(db, PLANS))]


def plan(db: DB, plan_id: str) -> Plan:
    return next(p for p in plans(db) if p.id == plan_id)


def lenders(db: DB, include_inactive: bool = False) -> list:
    query = select(Lender).order_by(Lender.created_at, Lender.id)
    if not include_inactive:
        query = query.where(Lender.active.is_(True))
    return [lender_out(row) for row in db.scalars(query)]


def market_prices(db: DB) -> list[MarketPrice]:
    prices = get_raw(db, MARKET) or []
    return [
        MarketPrice.model_validate({**price, "id": price.get("id") or f"legacy-market-{i}", "status": price.get("status") or "draft"})
        for i, price in enumerate(prices)
    ]


def weather_override(db: DB) -> Weather | None:
    value = get_raw(db, WEATHER_OVERRIDE)
    return Weather.model_validate(value) if value else None


def crops(db: DB) -> list[CropContent]:
    return [CropContent.model_validate(c) for c in get_raw(db, CROPS) or []]


def assistant_kb(db: DB) -> list[AssistantKnowledgeItem]:
    """Highest priority first; the fallback (no match) reply is always included."""
    items = get_raw(db, ASSISTANT_KB) or []
    if not any(i.get("id") == ASSISTANT_FALLBACK_ID for i in items):
        items.append(next(fresh(i) for i in DEFAULT_ASSISTANT_KB if i["id"] == ASSISTANT_FALLBACK_ID))
    return sorted((AssistantKnowledgeItem.model_validate(i) for i in items), key=lambda i: -i.priority)


def diagnoses(db: DB) -> list[DiagnosisContent]:
    return [DiagnosisContent.model_validate(d) for d in get_raw(db, DIAGNOSES) or []]
