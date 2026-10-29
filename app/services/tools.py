"""The farmer's AI tools: the assistant, the crop doctor and the yield predictor. Each checks the
feature flag and the plan, and the AI ones spend credits only when they succeed."""

from sqlalchemy.orm import Session as DB

from app.core.errors import AppError
from app.domain.assistant import AssistantContext, match_knowledge, resolve_reply
from app.domain.credit import calculate_credit_score
from app.domain.defaults import ASSISTANT_FALLBACK_ID
from app.domain.diagnosis import IMAGE_TYPES, MAX_IMAGE_BYTES, diagnose
from app.domain.farming import predict_yield
from app.domain.finance import total_expenses, total_income
from app.domain.market import published_prices
from app.integrations import weather as weather_api
from app.schemas import farm as f
from app.services import platform_store, subscriptions
from app.services.farmer_data import load_farmer

WEATHER_TOKENS = ("{weather}", "{rainAdvice}", "{fieldWorkAdvice}")


def farm_weather(profile, need: bool) -> dict | None:
    """Today's weather at the farm's pin, else in its region; None when unknown or unavailable."""
    if not need:
        return None
    if profile.gps:
        location = {"kind": "coordinates", "label": "Your farm", "latitude": profile.gps.lat, "longitude": profile.gps.lng}
    elif profile.region in weather_api.REGION_COORDS:
        location = {"kind": "place", "label": profile.region, "region": profile.region}
    else:
        return None
    try:
        report = weather_api.forecast(location)
    except weather_api.WeatherUnavailable:
        return None
    return {**report["current"], "storm": report["_storm"]}


def assistant_context(db: DB, user_id: str, *, with_weather: bool = False) -> AssistantContext:
    data = load_farmer(db, user_id)
    config = platform_store.credit_config(db)
    credit = calculate_credit_score(data.records, data.momo, data.profile, config)
    return AssistantContext(
        farmer_name=data.profile.name,
        region=data.profile.region,
        primary_crops=list(data.profile.primary_crops),
        score=credit.score,
        income=total_income(data.records),
        expenses=total_expenses(data.records),
        record_count=len(data.records),
        credit_factors=credit,
        bands=config.bands,
        ghana_card=data.profile.ghana_card.status,
        weather=farm_weather(data.profile, with_weather),
    )


def ask_assistant(db: DB, user_id: str, message: str) -> f.AssistantAnswer:
    hold = subscriptions.reserve(db, user_id, "aiAssistant")
    try:
        knowledge = platform_store.assistant_kb(db)
        # Weather is fetched only when the reply that answers will show it.
        item = match_knowledge(message, knowledge) or next(
            (k for k in knowledge if k.id == ASSISTANT_FALLBACK_ID), None
        )
        reply_text = str(item.reply.model_dump()) if item else ""
        _, reply = resolve_reply(
            message,
            assistant_context(db, user_id, with_weather=any(t in reply_text for t in WEATHER_TOKENS)),
            knowledge,
            platform_store.crops(db),
            published_prices(platform_store.market_prices(db)),
        )
    except Exception:
        subscriptions.settle(db, hold, success=False)
        raise
    snap = subscriptions.settle(db, hold, success=True)
    return f.AssistantAnswer(reply=reply, credits_remaining=snap.credits_remaining)


def scan_crop(db: DB, user_id: str, filename: str, content_type: str | None, size: int, crop: str | None) -> f.DiagnosisAnswer:
    if content_type not in IMAGE_TYPES:
        raise AppError("Upload a photo (JPEG, PNG, WebP or HEIC).")
    if size > MAX_IMAGE_BYTES:
        raise AppError("The photo is larger than 10 MB. Choose a smaller one.")
    hold = subscriptions.reserve(db, user_id, "cropDoctor")
    try:
        result = diagnose(filename, size, platform_store.diagnoses(db), crop)
    except ValueError as error:
        subscriptions.settle(db, hold, success=False)
        raise AppError(str(error)) from error
    snap = subscriptions.settle(db, hold, success=True)
    return f.DiagnosisAnswer(result=result, credits_remaining=snap.credits_remaining)


def predict(db: DB, user_id: str, data: f.YieldInput) -> f.YieldResult:
    subscriptions.assert_feature(db, user_id, "yieldPredictor")
    return f.YieldResult(**predict_yield(data, platform_store.crops(db)))
