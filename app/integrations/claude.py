"""Claude (Anthropic): the assistant's answers and the crop doctor's reading of a photo.

Both come back in the app's existing shapes (an AssistantReply, a DiagnosisResult) through
structured outputs, so the frontend shows them as before. Only what a request needs is sent:
the farmer's summary figures for the assistant, the photo for the crop doctor. A declined
request is retried server-side on the model Anthropic recommends (`fallbacks: "default"`)."""

import base64
import io
import json
import logging
from dataclasses import dataclass

import anthropic
from PIL import Image, UnidentifiedImageError

from app.config import get_settings

log = logging.getLogger("agrictec.claude")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept `fallbacks: "default"` (Claude Haiku 5.5 has no server-side fallback).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
# Photos are scaled down to this long edge first: enough detail for leaves and spots, and well
# inside the API's image limits.
MAX_EDGE = 1568
# What Claude reads directly; HEIC (iPhone) has to be converted on the phone first.
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
# Kept under the web app's 120 s proxy timeout, retries included.
TIMEOUT_SECONDS = 50.0


class AIUnavailable(Exception):
    """Claude couldn't answer (not configured, unreachable, declined or cut short)."""


class NotACropPhoto(Exception):
    """The photo doesn't show a plant Claude can assess."""


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    key = get_settings().anthropic_api_key
    if not key:
        raise AIUnavailable("Claude is not configured.")
    if _client is None or _client.api_key != key:
        _client = anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=1)
    return _client


def _structured(*, system: str, messages: list[dict], schema: dict, effort: str, max_tokens: int) -> dict:
    """One request whose answer must match `schema`; its parsed JSON."""
    model = get_settings().anthropic_model
    extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    try:
        response = _get_client().beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            **extra,
        )
    except anthropic.RateLimitError as error:
        raise AIUnavailable("Claude is busy right now.") from error
    except anthropic.APIStatusError as error:
        log.warning("Claude request failed: %s %s", error.status_code, error.message)
        raise AIUnavailable("Claude couldn't answer.") from error
    except anthropic.APIConnectionError as error:
        raise AIUnavailable("Claude couldn't be reached.") from error
    if response.stop_reason != "end_turn":
        # "refusal" (after any fallback) or "max_tokens": no complete answer to show.
        log.info("Claude stopped with %s", response.stop_reason)
        raise AIUnavailable("Claude couldn't answer that.")
    text = next((block.text for block in response.content if block.type == "text"), None)
    try:
        return json.loads(text or "")
    except json.JSONDecodeError as error:
        raise AIUnavailable("Claude's answer couldn't be read.") from error


# ---------- The assistant ----------

ASSISTANT_SYSTEM = """You are the AgriCtec farm assistant, inside an app that smallholder farmers in \
Ghana use to keep farm records, follow their AgriCtec score and find loans from verified lenders.

Answer the farmer's question in plain English that a farmer reading on a phone can follow, with \
money in Ghana cedis (GHS). Use the farmer's data below where it helps, and only that data: never \
make up records, prices, weather or loan offers you weren't given. If the question needs \
something you don't know (a local price, a diagnosis you can't see), say so and suggest the \
local agricultural extension officer, or the app's Crop Doctor for a photo of a sick plant.

How the app works, for questions about it: the AgriCtec score (300-850) is the app's own \
measure of record keeping, not a credit bureau score. It comes from payment history, how complete \
the farm records are, income stability, mobile money activity and verification, and keeping \
records regularly is the surest way to raise it. The score never decides a loan: each lender sets \
its own requirements, shown with its loan products in the app, and applying needs a verified \
Ghana Card. Never say the farmer qualifies, is eligible or can borrow a given amount, and never \
promise that a loan will be approved.

Reply in this shape: a short title; an intro of one or two sentences; up to five bullets of \
concrete steps or facts (an empty list when they wouldn't help); and a tip, or an empty string. \
If a prepared answer from the platform is given, keep its facts where they fit the question."""

ASSISTANT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "tip": {"type": "string"},
    },
    "required": ["title", "intro", "bullets", "tip"],
    "additionalProperties": False,
}


def _reply_text(reply: dict) -> str:
    parts = [reply.get("title"), reply.get("intro"), *(reply.get("bullets") or []), reply.get("tip")]
    return "\n".join(p for p in parts if p)


def _history(turns: list[tuple[str, object]], message: str) -> list[dict]:
    """The recent conversation as API messages: it must start with the farmer, and ends with
    the new question. Saved assistant replies are structured; they're sent as plain text."""
    messages: list[dict] = []
    for role, content in turns:
        text = content if isinstance(content, str) else _reply_text(content if isinstance(content, dict) else {})
        if not text.strip():
            continue
        api_role = "user" if role == "user" else "assistant"
        if not messages and api_role == "assistant":
            continue
        messages.append({"role": api_role, "content": text})
    messages.append({"role": "user", "content": message})
    return messages


def assistant_reply(*, farmer_facts: str, prepared: dict | None, turns: list[tuple[str, object]], message: str) -> dict:
    """{title, intro, bullets, tip}, with empty fields left out."""
    system = f"{ASSISTANT_SYSTEM}\n\nThe farmer's data:\n{farmer_facts}"
    if prepared:
        system += f"\n\nThe platform's prepared answer for this question:\n{_reply_text(prepared)}"
    answer = _structured(
        system=system,
        messages=_history(turns, message),
        schema=ASSISTANT_SCHEMA,
        effort="low",
        max_tokens=4000,
    )
    title = (answer.get("title") or "").strip()[:120] or "Farm assistant"
    reply: dict = {"title": title}
    intro = (answer.get("intro") or "").strip()[:600]
    bullets = [b.strip()[:400] for b in answer.get("bullets") or [] if b.strip()][:6]
    tip = (answer.get("tip") or "").strip()[:400]
    if intro:
        reply["intro"] = intro
    if bullets:
        reply["bullets"] = bullets
    if tip:
        reply["tip"] = tip
    return reply


# ---------- The crop doctor ----------

DOCTOR_SYSTEM = """You are a plant-health assistant for smallholder farmers in Ghana, looking at \
a photo a farmer took of a crop. Common crops are maize, cassava, cocoa, yam, rice, tomato, \
plantain, groundnut, pepper and cowpea.

Say whether the photo shows a plant you can assess. If it does, name the most likely problem \
(a disease, a pest, a nutrient deficiency) or "Healthy" when you see none, with how sure you \
are from 0 to 100. Be honest about uncertainty: a blurry or distant photo deserves low \
confidence. Severity is "None" for a healthy plant, else "Moderate" or "High". The treatment \
is one or two sentences of practical steps a farmer in Ghana can take, naming products by \
their active ingredient and saying to follow the label; prevention is up to four short steps. \
When the photo shows no plant you can assess, set isPlant to false and leave the other text \
fields empty."""

DOCTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "isPlant": {"type": "boolean"},
        "crop": {"type": "string"},
        "disease": {"type": "string"},
        "severity": {"type": "string", "enum": ["None", "Moderate", "High"]},
        "confidence": {"type": "number"},
        "treatment": {"type": "string"},
        "prevention": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["isPlant", "crop", "disease", "severity", "confidence", "treatment", "prevention"],
    "additionalProperties": False,
}


@dataclass
class Photo:
    media_type: str
    data: str  # base64


def prepare_photo(content: bytes, content_type: str | None) -> Photo:
    """The photo as Claude will see it: scaled to MAX_EDGE, re-encoded as JPEG."""
    if content_type not in PHOTO_TYPES:
        raise ValueError("Use a JPEG, PNG or WebP photo for the crop doctor.")
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("That file couldn't be read as a photo.") from error
    image = image.convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return Photo("image/jpeg", base64.standard_b64encode(out.getvalue()).decode("ascii"))


THUMB_EDGE = 200


def thumbnail(photo: Photo) -> str:
    """A small JPEG data URL of the photo, kept with the saved diagnosis (made here, never taken
    from the browser, so it is always an inline image)."""
    image = Image.open(io.BytesIO(base64.standard_b64decode(photo.data)))
    image = image.convert("RGB")
    image.thumbnail((THUMB_EDGE, THUMB_EDGE))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.standard_b64encode(out.getvalue()).decode("ascii")


def diagnose_photo(photo: Photo, crop: str | None) -> dict:
    """{crop, disease, severity, confidence (0-100), treatment, prevention}."""
    hint = f"The farmer says this is {crop}." if crop else "The farmer didn't say which crop this is."
    answer = _structured(
        system=DOCTOR_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": photo.media_type, "data": photo.data}},
                {"type": "text", "text": f"{hint} What's wrong with this plant, if anything?"},
            ],
        }],
        schema=DOCTOR_SCHEMA,
        effort="medium",
        max_tokens=4000,
    )
    if not answer.get("isPlant") or not (answer.get("disease") or "").strip():
        raise NotACropPhoto("That photo doesn't show a plant the crop doctor can assess.")
    prevention = [p.strip()[:200] for p in answer.get("prevention") or [] if p.strip()][:4]
    return {
        "crop": (((answer.get("crop") or "").strip() or crop or "")[:40] or None),
        "disease": answer["disease"].strip()[:80],
        "severity": answer.get("severity") if answer.get("severity") in ("None", "Moderate", "High") else "Moderate",
        "confidence": round(min(100.0, max(0.0, float(answer.get("confidence") or 0)))),
        "treatment": (answer.get("treatment") or "").strip()[:1000] or "Ask your extension officer to confirm.",
        "prevention": prevention or None,
    }
