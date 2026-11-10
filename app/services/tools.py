"""The farmer's AI tools: the assistant, the crop doctor and the yield predictor. Each checks the
feature flag and the plan, and the AI ones spend credits only when they succeed."""

import re
import uuid
from typing import get_args

from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.errors import AppError
from app.domain.assistant import AssistantContext, lock_loans, match_knowledge, resolve_reply
from app.domain.clock import now_utc
from app.domain.constants import Crop
from app.domain.credit import calculate_credit_score, score_label
from app.domain.defaults import ASSISTANT_FALLBACK_ID
from app.domain.diagnosis import IMAGE_TYPES, MAX_IMAGE_BYTES
from app.domain.farming import predict_yield
from app.domain.finance import total_expenses, total_income
from app.domain.market import published_prices, unit_label
from app.integrations import claude
from app.integrations import weather as weather_api
from app.schemas import farm as f
from app.schemas.platform import AssistantReply, MarketPrice
from app.services import farm, platform_store, subscriptions
from app.services.farmer_data import load_farmer

WEATHER_TOKENS = ("{weather}", "{rainAdvice}", "{fieldWorkAdvice}")
# With Claude answering, questions like these also get today's weather in the farmer's data.
WEATHER_WORDS = re.compile(
    r"\b(weather|rain\w*|storm|wind\w*|dry|spray\w*|plant\w*|sow\w*|harvest\w*|today|tomorrow)\b", re.I
)
# Earlier messages Claude sees, so follow-up questions make sense.
CHAT_TURNS = 10
CROPS = {c.lower(): c for c in get_args(Crop)}


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


def farmer_facts(ctx: AssistantContext, prices: list[MarketPrice]) -> str:
    """What Claude is told about the farmer: summary figures, never individual records."""
    lines = [
        f"- Name: {ctx.farmer_name or 'not given'}; region: {ctx.region or 'not given'}",
        f"- Main crops: {', '.join(ctx.primary_crops) or 'not given'}",
        f"- Credit score: {ctx.score} ({score_label(ctx.score, ctx.bands)})",
        f"- Farm records: {ctx.record_count}; total income GHS {ctx.income:,.2f}, total expenses "
        f"GHS {ctx.expenses:,.2f}, profit GHS {ctx.income - ctx.expenses:,.2f}",
        f"- Ghana Card: {ctx.ghana_card or 'not submitted'}",
    ]
    if ctx.credit_factors:
        c = ctx.credit_factors
        lines.append(
            f"- Score factors (0-100): payment history {c.payment_history}, record completeness "
            f"{c.record_completeness}, income stability {c.income_stability}, mobile money "
            f"{c.momo_activity}, verification {c.verification}"
        )
    if ctx.weather:
        w = ctx.weather
        lines.append(
            f"- Weather at the farm now: {w.get('description')}, {w.get('temp')}°C, humidity "
            f"{w.get('humidity')}%, "
            + (f"rain chance {w['rainChance']}%" if w.get("rainChance") is not None else f"rain {w.get('rainMm') or 0} mm this hour (no rain probability given)")
            + f", wind {w.get('windKph')} km/h"
            + ("; a storm is forecast" if w.get("storm") else "")
        )
    own = {c.lower() for c in ctx.primary_crops}
    relevant = [p for p in prices if p.crop.lower() in own] or prices
    for p in relevant[:8]:
        where = p.market or p.region or "Ghana"
        lines.append(f"- Published price: {p.crop}, GHS {p.price:,.2f} per {unit_label(p)} ({where})")
    return "\n".join(lines)


def _save_exchange(db: DB, user_id: str, message: str, reply: AssistantReply) -> list[f.ChatMessage]:
    """The question and the reply, appended to the conversation by the API itself (the browser
    can't write the conversation: Claude reads it back as earlier turns)."""
    now = now_utc()
    asked = f.ChatMessage(id=f"u-{uuid.uuid4().hex}", role="user", content=message, created_at=now)
    answered = f.ChatMessage(id=f"a-{uuid.uuid4().hex}", role="ai", content=reply, created_at=now)
    farm.append_chat(db, user_id, [asked, answered])
    return [asked, answered]


def _recent_turns(db: DB, user_id: str) -> list[tuple[str, object]]:
    turns = farm.list_chat(db, user_id)[-CHAT_TURNS:]
    return [
        (t.role, t.content if isinstance(t.content, str) else t.content.model_dump(exclude_none=True))
        for t in turns
    ]


def ask_assistant(db: DB, user_id: str, message: str) -> f.AssistantAnswer:
    """Claude's answer when configured, else (or when Claude can't answer) the knowledge base's."""
    hold = subscriptions.reserve(db, user_id, "aiAssistant")
    try:
        ai = get_settings().ai_enabled
        knowledge = platform_store.assistant_kb(db)
        # Weather is fetched only when the answer may use it.
        item = match_knowledge(message, knowledge) or next(
            (k for k in knowledge if k.id == ASSISTANT_FALLBACK_ID), None
        )
        reply_text = str(item.reply.model_dump()) if item else ""
        need_weather = any(t in reply_text for t in WEATHER_TOKENS) or bool(ai and WEATHER_WORDS.search(message))
        ctx = assistant_context(db, user_id, with_weather=need_weather)
        prices = published_prices(platform_store.market_prices(db))
        matched, reply = resolve_reply(message, ctx, knowledge, platform_store.crops(db), prices)
        source = "rules"
        if ai:
            try:
                answer = claude.assistant_reply(
                    farmer_facts=farmer_facts(ctx, prices),
                    prepared=reply.model_dump(exclude_none=True) if matched else None,
                    turns=_recent_turns(db, user_id),
                    message=message,
                )
                reply, source = lock_loans(AssistantReply(**answer), ctx), "ai"
            except claude.AIUnavailable:
                pass  # The knowledge base's reply stands.
        messages = _save_exchange(db, user_id, message, reply)
    except Exception:
        subscriptions.settle(db, hold, success=False)
        raise
    snap = subscriptions.settle(db, hold, success=True)
    return f.AssistantAnswer(reply=reply, credits_remaining=snap.credits_remaining, source=source, messages=messages)


def scan_crop(db: DB, user_id: str, filename: str, content_type: str | None, content: bytes, crop: str | None) -> f.DiagnosisAnswer:
    """Claude reads the photo; without it the crop doctor is unavailable (it never makes up a
    diagnosis). The diagnosis is saved to the farmer's history here, with a thumbnail made here.
    Credits are spent only when a diagnosis is produced."""
    if not get_settings().ai_enabled:
        raise AppError(
            "The crop doctor isn't available right now. Please try again later.",
            code="ai_unavailable",
            status_code=503,
        )
    if content_type not in IMAGE_TYPES:
        raise AppError("Upload a photo (JPEG, PNG, WebP or HEIC).")
    if len(content) > MAX_IMAGE_BYTES:
        raise AppError("The photo is larger than 10 MB. Choose a smaller one.")
    try:
        photo = claude.prepare_photo(content, content_type)
    except ValueError as error:
        raise AppError(str(error)) from error
    hold = subscriptions.reserve(db, user_id, "cropDoctor")
    try:
        found = claude.diagnose_photo(photo, crop)
        # Saved diagnoses take the app's crop names only.
        found["crop"] = crop if crop in CROPS.values() else CROPS.get((found["crop"] or "").lower())
        result = f.DiagnosisResultOut(**found, source="ai")
        saved = farm.create_diagnosis(
            db,
            user_id,
            f.DiagnosisInput(
                disease=result.disease,
                severity=result.severity,
                confidence=result.confidence,
                treatment=result.treatment,
                prevention=result.prevention,
                crop=result.crop,
                image_thumb=claude.thumbnail(photo),
                date=now_utc(),
                source="ai",
            ),
        )
    except claude.NotACropPhoto as error:
        subscriptions.settle(db, hold, success=False)
        raise AppError(f"{error} Take a closer photo of the affected leaves or fruit.") from error
    except claude.AIUnavailable as error:
        subscriptions.settle(db, hold, success=False)
        raise AppError(
            "The crop doctor couldn't look at the photo just now. Try again in a moment; no credits were used.",
            code="ai_unavailable",
            status_code=503,
        ) from error
    except ValueError as error:
        subscriptions.settle(db, hold, success=False)
        raise AppError(str(error)) from error
    except Exception:
        subscriptions.settle(db, hold, success=False)
        raise
    snap = subscriptions.settle(db, hold, success=True)
    return f.DiagnosisAnswer(result=result, credits_remaining=snap.credits_remaining, diagnosis=saved)


def predict(db: DB, user_id: str, data: f.YieldInput) -> f.YieldResult:
    subscriptions.assert_feature(db, user_id, "yieldPredictor")
    return f.YieldResult(**predict_yield(data, platform_store.crops(db)))
