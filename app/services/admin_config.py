"""Credit rules, feature flags, plans and content (services/admin/config.service.ts,
services/admin/content.service.ts). Every save is validated, audited and broadcast."""

from pydantic import ValidationError
from sqlalchemy.orm import Session as DB

from app.core.audit import append_audit
from app.core.errors import AppError, NotFound
from app.core.events import bus
from app.domain.assistant import AssistantContext, matched_keyword, resolve_reply
from app.domain.clock import now_utc, today
from app.domain.credit_rules import ScoringInput, impact_of
from app.domain.defaults import ASSISTANT_FALLBACK_ID
from app.domain.market import publication_error, published_prices
from app.domain.plans import normalize_plans
from app.models import Admin, CreditConfigVersion
from app.schemas.platform import (
    AssistantKnowledgeItem,
    CreditConfig,
    CreditConfigInput,
    CropContent,
    DiagnosisContent,
    FeatureFlags,
    MarketPrice,
    Plan,
    PlanList,
    Weather,
)
from app.services import platform_store as store
from app.services import tools
from app.services.farmer_data import load_farmers

# ---------- Credit rules ----------


def _clean_note(note: str) -> str:
    """A publish note of at least 10 characters, so the history explains every change."""
    clean = " ".join(note.split())
    if len(clean) < 10:
        raise AppError("Add a change note of at least 10 characters.")
    if len(clean) > 500:
        raise AppError("Keep the change note under 500 characters.")
    return clean


def _write_version(
    db: DB, actor: Admin, values: CreditConfigInput, *, note: str | None = None, restored_from: int | None = None
) -> CreditConfig:
    """The next version; every earlier one is kept for good."""
    row = CreditConfigVersion(
        version=store.max_credit_version(db) + 1,
        updated_at=now_utc(),
        updated_by=actor.name,
        note=note,
        restored_from=restored_from,
        **values.values(),
    )
    db.add(row)
    db.flush()
    return store.credit_config(db)


def save_credit_config(db: DB, actor: Admin, values: CreditConfigInput) -> CreditConfig:
    current = store.credit_config(db)
    saved = _write_version(db, actor, values)
    append_audit(
        db, actor, action="credit.update", target_type="config", target_id="credit-config",
        summary=f"Saved credit rules as version {saved.version}",
        before=current.values(), after={"version": saved.version, **saved.values()},
    )
    db.commit()
    bus.publish("platform")
    return saved


def publish_credit_config(db: DB, actor: Admin, values: CreditConfigInput, note: str) -> CreditConfig:
    """A new version with the change note; farmers' scores follow at once."""
    current = store.credit_config(db)
    clean = _clean_note(note)
    if values.values() == current.values():
        raise AppError("Nothing changed: the draft matches the live rules.")
    saved = _write_version(db, actor, values, note=clean)
    append_audit(
        db, actor, action="credit.publish", target_type="config", target_id="credit-config",
        summary=f"Published credit rules version {saved.version}: {clean}",
        before={"version": current.version, **current.values()},
        after={"version": saved.version, "note": clean, **saved.values()},
    )
    db.commit()
    bus.publish("platform")
    return saved


def restore_credit_version(db: DB, actor: Admin, version: int, note: str | None) -> CreditConfig:
    """Saves the old values as a NEW version, so nothing in the history is rewritten or lost."""
    current = store.credit_config(db)
    if version == current.version:
        raise AppError("That version is already live.")
    target = db.get(CreditConfigVersion, version)
    if target is None:
        raise NotFound(f"Version {version} was not found.")
    clean = _clean_note(note) if note and note.strip() else f"Restored version {version}."
    values = CreditConfigInput.model_validate(
        {"weights": target.weights, "targets": target.targets, "bands": target.bands, "eligibility": target.eligibility}
    )
    saved = _write_version(db, actor, values, note=clean, restored_from=version)
    append_audit(
        db, actor, action="credit.restore", target_type="config", target_id="credit-config",
        summary=f"Restored credit rules from version {version}",
        before={"version": current.version, **current.values()},
        after={"version": saved.version, "restoredFrom": version, **saved.values()},
    )
    db.commit()
    bus.publish("platform")
    return saved


def scoring_inputs(db: DB) -> list[ScoringInput]:
    """What the score needs for every registered farmer."""
    return [
        ScoringInput(
            user_id=uid,
            name=d.profile.name or d.user.email,
            region=d.profile.region,
            records=d.records,
            momo=d.momo,
            profile=d.profile,
        )
        for uid, d in load_farmers(db).items()
        if not d.user.is_guest
    ]


def impact(db: DB, draft: dict) -> dict:
    """Re-scores every farmer under the live rules and the draft. Nothing is saved."""
    try:
        values = CreditConfigInput.model_validate(draft)
    except ValidationError as error:
        raise AppError(error.errors()[0]["msg"].removeprefix("Value error, ")) from error
    return impact_of(scoring_inputs(db), store.credit_config(db), values)


# ---------- Flags and plans ----------


def save_flags(db: DB, actor: Admin, flags: FeatureFlags) -> FeatureFlags:
    before = store.flags(db)
    old, new = before.model_dump(by_alias=True), flags.model_dump(by_alias=True)
    changed = [key for key in new if new[key] != old.get(key)]
    store.put_raw(db, store.FLAGS, flags.model_dump(mode="json", by_alias=True), actor.name)
    append_audit(
        db, actor, action="settings.flags", target_type="system", target_id="feature-flags",
        summary=f"Changed feature flags: {', '.join(changed)}" if changed else "Saved feature flags (no changes)",
        before={k: old.get(k) for k in changed}, after={k: new[k] for k in changed},
    )
    db.commit()
    bus.publish("platform")
    return store.flags(db)


def save_plans(db: DB, actor: Admin, plans: list[Plan]) -> list[Plan]:
    valid = PlanList(plans=plans).plans
    before = store.plans(db)
    normalized = normalize_plans([p.model_dump(mode="json", by_alias=True, exclude_unset=True) for p in valid])
    store.put_raw(db, store.PLANS, normalized, actor.name)
    append_audit(
        db, actor, action="settings.plans", target_type="system", target_id="plans",
        summary="Updated subscription plans", before=before, after=normalized,
    )
    db.commit()
    bus.publish("platform")
    return store.plans(db)


# ---------- Content ----------


def _save_content(db: DB, actor: Admin, key: str, value, action: str, summary: str):
    before = store.get_raw(db, key)
    store.put_raw(db, key, value, actor.name)
    append_audit(
        db, actor, action=action, target_type="content", target_id=key.replace("platform:content:", ""),
        summary=summary, before=before, after=value,
    )
    db.commit()
    bus.publish("platform")


def _dump(items) -> list[dict]:
    return [i.model_dump(mode="json", by_alias=True, exclude_none=True) for i in items]


def save_market_prices(db: DB, actor: Admin, prices: list[MarketPrice]) -> list[MarketPrice]:
    """Published observations must pass the evidence rules; drafts may be incomplete."""
    if len(prices) > 1000:
        raise AppError("Keep the list under 1,000 observations.")
    ids = [p.id for p in prices if p.id]
    if len(ids) != len(set(ids)):
        raise AppError("Each observation needs its own ID.")
    for price in prices:
        if price.status == "published":
            error = publication_error(price, today())
            if error:
                raise AppError(error)
    _save_content(db, actor, store.MARKET, _dump(prices), "content.market", f"Updated {len(prices)} market prices")
    return store.market_prices(db)


def save_weather_override(db: DB, actor: Admin, weather: Weather | None) -> Weather | None:
    """Kept for compatibility: farmers never see an override as live weather."""
    value = weather.model_dump(mode="json", by_alias=True) if weather else None
    summary = (
        f"Set the weather to “{weather.description}”, {weather.temp:g}°C" if weather else "Cleared the weather override"
    )
    _save_content(
        db, actor, store.WEATHER_OVERRIDE, value,
        "content.weather_override" if weather else "content.weather_clear", summary,
    )
    return store.weather_override(db)


def save_crops(db: DB, actor: Admin, crops: list[CropContent]) -> list[CropContent]:
    _save_content(db, actor, store.CROPS, _dump(crops), "content.crops", "Updated crop yields, prices and tips")
    return store.crops(db)


def save_assistant_kb(db: DB, actor: Admin, items: list[AssistantKnowledgeItem]) -> list[AssistantKnowledgeItem]:
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        raise AppError("Each assistant reply needs a unique ID.")
    if ASSISTANT_FALLBACK_ID not in ids:
        raise AppError("The fallback reply can’t be deleted.")
    _save_content(
        db, actor, store.ASSISTANT_KB, _dump(items), "content.assistant", f"Updated the assistant’s {len(items)} replies"
    )
    return store.assistant_kb(db)


def save_diagnoses(db: DB, actor: Admin, items: list[DiagnosisContent]) -> list[DiagnosisContent]:
    if not any(d.active for d in items):
        raise AppError("Keep at least one diagnosis active, or the crop doctor has nothing to show.")
    _save_content(db, actor, store.DIAGNOSES, _dump(items), "content.diagnoses", f"Updated {len(items)} crop diagnoses")
    return store.diagnoses(db)


def test_assistant(db: DB, message: str, farmer_id: str | None) -> dict:
    """What the assistant would answer, for one farmer or a sample farmer (no credits spent)."""
    ctx = (
        tools.assistant_context(db, farmer_id)
        if farmer_id
        else AssistantContext(
            farmer_name="Ama", region="Ashanti", primary_crops=["Maize"], score=640, income=24_000,
            expenses=15_000, record_count=18, bands=store.credit_config(db).bands,
        )
    )
    item, reply = resolve_reply(
        message, ctx, store.assistant_kb(db), store.crops(db), published_prices(store.market_prices(db))
    )
    return {
        "itemId": item.id if item else None,
        "matchedKeyword": matched_keyword(item, message) if item else None,
        "reply": reply.model_dump(by_alias=True, exclude_none=True),
    }

