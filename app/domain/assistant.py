"""The rule-based AI assistant (lib/ai/assistant-replies.ts).

The router walks the admin's active knowledge items by priority and answers with the first whose
keywords match, then fills in {placeholders} with the farmer's own data. A keyword matches when
every word in it appears in the message; a word ending in * matches any word starting with it
("maize plant*" matches "when do I plant maize?" and "maize planting").

To use a real LLM later, replace resolve_reply: callers keep the same input and output."""

import re
from dataclasses import dataclass, field

from app.domain.credit import score_label
from app.domain.defaults import ASSISTANT_FALLBACK_ID
from app.domain.fmt import format_ghs
from app.domain.loans import LOANS_LOCKED_HINT
from app.domain.market import format_market_amount, unit_label
from app.schemas.farm import CreditScoreBreakdown
from app.schemas.platform import (
    AssistantKnowledgeItem,
    AssistantReply,
    CreditBands,
    CropContent,
    MarketPrice,
)

_FALLBACK = AssistantReply(
    title="Let me help",
    intro="For advice specific to this issue, contact your local MoFA extension officer.",
    bullets=[
        "Maize planting seasons and cassava tips.",
        "Fall armyworm, pests and fertilizer guidance.",
        "Weather information and indicative cocoa prices.",
        "Your credit score, farm income, expenses and profit.",
    ],
    tip="Tell me the crop and the issue, or choose a quick prompt below.",
)

_NORTHERN = re.compile(r"northern|savannah|north east|upper east|upper west", re.I)
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_TOKEN = re.compile(r"\{([\w:]+)\}")
_LOAN_WORDS = re.compile(r"\bloans?\b|\bborrow", re.I)


@dataclass
class AssistantContext:
    farmer_name: str = ""
    region: str = ""
    primary_crops: list[str] = field(default_factory=list)
    score: int = 300
    income: float = 0
    expenses: float = 0
    record_count: int = 0
    credit_factors: CreditScoreBreakdown | None = None
    bands: CreditBands | None = None
    # Unless 'verified', a reply about loans ends with how to unlock them.
    ghana_card: str | None = None
    # Today's weather where the farmer farms (None when no provider answered).
    weather: dict | None = None


def words_of(text: str) -> list[str]:
    return _WORDS.findall(text.lower())


def keyword_matches(keyword: str, words: list[str]) -> bool:
    terms = keyword.lower().split()
    return bool(terms) and all(
        any(w.startswith(t[:-1]) for w in words) if t.endswith("*") else t in words for t in terms
    )


def match_knowledge(message: str, knowledge: list[AssistantKnowledgeItem]) -> AssistantKnowledgeItem | None:
    """The first active item (highest priority first) with a matching keyword."""
    words = words_of(message)
    ordered = sorted((i for i in knowledge if i.active), key=lambda i: -i.priority)
    return next((i for i in ordered if any(keyword_matches(k, words) for k in i.keywords)), None)


def matched_keyword(item: AssistantKnowledgeItem, message: str) -> str | None:
    words = words_of(message)
    return next((k for k in item.keywords if keyword_matches(k, words)), None)


def _credit_steps(ctx: AssistantContext, name: str) -> tuple[str | None, list[str]]:
    factors = [
        ("payment_history", "Payment History", "Record genuine farm or MoMo activity each month to build a consistent six-month history."),
        (
            "record_completeness",
            "Record Completeness",
            f"You have {ctx.record_count} farm records. Log every sale, expense and activity, and work towards 30 combined farm and MoMo entries.",
        ),
        (
            "income_stability",
            "Income Stability",
            f"With {format_ghs(ctx.income)} income and {format_ghs(ctx.expenses)} expenses, "
            + (
                "protect your surplus by budgeting for the next season"
                if ctx.income > ctx.expenses
                else "review your largest costs and plan sales to improve your surplus"
            )
            + ".",
        ),
        ("momo_activity", "MoMo Activity", "Log genuine farm-related Mobile Money receipts and payments; the activity factor reaches its maximum at 20 entries."),
        (
            "verification",
            "Verification",
            f"{name}, complete your name, phone, farm name and acreage, then review phone, location and Ghana Card verification in your profile.",
        ),
    ]
    credit = ctx.credit_factors
    if credit:
        factors.sort(key=lambda f: getattr(credit, f[0]))
        weakest = f"{factors[0][1]} ({getattr(credit, factors[0][0])}%)"
        return weakest, [f"{label} ({getattr(credit, key)}%): {step}" for key, label, step in factors]
    return None, [step for _, _, step in factors]


def _placeholder_values(
    tokens: set[str], ctx: AssistantContext, crops: list[CropContent], prices: list[MarketPrice]
) -> dict[str, str | list[str]]:
    name = ctx.farmer_name.strip() or "Farmer"
    region = ctx.region.strip() or "your region"
    profit = ctx.income - ctx.expenses
    weakest, steps = _credit_steps(ctx, name)
    values: dict[str, str | list[str]] = {
        "name": name,
        "region": region,
        "crops": ", ".join(ctx.primary_crops) if ctx.primary_crops else "your crops",
        "score": str(ctx.score),
        "scoreLabel": score_label(ctx.score, ctx.bands),
        "creditFocus": (
            f"Your weakest factor is {weakest}. Start with the first step below."
            if weakest
            else "These five steps help build your farm financial history."
        ),
        "creditSteps": steps,
        "income": format_ghs(ctx.income),
        "expenses": format_ghs(ctx.expenses),
        "profit": format_ghs(profit),
        "margin": f"{profit / ctx.income * 100:.1f}%" if ctx.income > 0 else "not available (no income recorded)",
        "records": str(ctx.record_count),
        "financeTip": (
            "Add your first sale and expense in Farm Records to start tracking your profit."
            if ctx.record_count == 0
            else "Review your largest expense category and compare input prices before your next purchase."
            if profit <= 0
            else "Set aside part of your surplus for next season’s inputs before taking on new spending."
        ),
        "plantingWindow": (
            "plan around the May–June window and confirm rainfall onset with your local MoFA extension officer"
            if _NORTHERN.search(region)
            else "confirm your local rainfall pattern with a MoFA extension officer; coastal areas can differ from forest and transitional zones"
        ),
        "cropWork": f" or scheduling work on {', '.join(ctx.primary_crops)}" if ctx.primary_crops else "",
    }
    values.update(_weather_values(ctx.weather))
    for token in tokens:
        if token.startswith("tips:"):
            crop = token[5:].lower()
            values[token] = next((list(c.tips) for c in crops if c.crop.lower() == crop), [])
    if "cocoaPrice" in tokens:
        cocoa = next((p for p in prices if p.crop == "Cocoa"), None)
        values["cocoaPrice"] = (
            f"Cocoa: {format_market_amount(cocoa.price)} per {unit_label(cocoa)}; "
            f"{cocoa.market or cocoa.region or 'source-reported coverage'}, observed {cocoa.observed_at}. "
            f"Source: {cocoa.source.publisher if cocoa.source else ''}."
            if cocoa
            else "Market price data unavailable. No published, reviewed cocoa observation is available."
        )
    return values


def _weather_values(weather: dict | None) -> dict[str, str]:
    """{weather}, {rainAdvice} and {fieldWorkAdvice}. Without a reading, the assistant says so:
    it never invents conditions."""
    if not weather:
        return {
            "weather": "Weather data unavailable. Open the Weather page to select your location.",
            "rainAdvice": "Location-specific rainfall advice is unavailable right now.",
            "fieldWorkAdvice": "Check a trusted local forecast before planning weather-sensitive field work.",
        }
    return {
        "weather": (
            f"{weather['description']}, {weather['temp']:g}°C, {weather['humidity']}% humidity and "
            f"{weather['rainChance']}% chance of rain"
        ),
        "rainAdvice": (
            "Check field drainage and postpone spraying if rain is expected."
            if weather["rainChance"] >= 60
            else "Check soil moisture and mulch to reduce water loss; water young plants where needed."
        ),
        "fieldWorkAdvice": (
            "Stay out of exposed fields during thunderstorms and secure loose equipment."
            if weather.get("storm")
            else "Plan demanding field work for cooler morning hours and keep drinking water nearby."
        ),
    }


def _tokens_in(reply: AssistantReply) -> set[str]:
    text = " ".join(filter(None, [reply.title, reply.intro, *(reply.bullets or []), reply.tip]))
    return set(_TOKEN.findall(text))


def _fill(text: str, values: dict[str, str | list[str]]) -> str:
    """Unknown placeholders stay as written, so an admin can spot a typo."""

    def sub(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        if value is None:
            return match.group(0)
        return " ".join(value) if isinstance(value, list) else value

    return _TOKEN.sub(sub, text)


def render(reply: AssistantReply, values: dict[str, str | list[str]]) -> AssistantReply:
    """Fills a reply. A bullet that is only a list placeholder ({creditSteps}) becomes several."""
    bullets: list[str] = []
    for bullet in reply.bullets or []:
        whole = re.fullmatch(r"\{([\w:]+)\}", bullet.strip())
        value = values.get(whole.group(1)) if whole else None
        bullets += value if isinstance(value, list) else [_fill(bullet, values)]
    intro = _fill(reply.intro, values) if reply.intro is not None else None
    tip = _fill(reply.tip, values) if reply.tip is not None else None
    return AssistantReply.model_construct(
        title=_fill(reply.title, values), intro=intro or None, bullets=bullets or None, tip=tip or None
    )


def _lock_loans(reply: AssistantReply, ctx: AssistantContext) -> AssistantReply:
    if not ctx.ghana_card or ctx.ghana_card == "verified":
        return reply
    text = " ".join(filter(None, [reply.title, reply.intro, *(reply.bullets or []), reply.tip]))
    return reply.model_copy(update={"tip": LOANS_LOCKED_HINT}) if _LOAN_WORDS.search(text) else reply


def resolve_reply(
    message: str,
    ctx: AssistantContext,
    knowledge: list[AssistantKnowledgeItem],
    crops: list[CropContent],
    prices: list[MarketPrice],
) -> tuple[AssistantKnowledgeItem | None, AssistantReply]:
    """The reply for a message and the item that answered (None for the fallback)."""
    item = match_knowledge(message, knowledge)
    fallback = next((k.reply for k in knowledge if k.id == ASSISTANT_FALLBACK_ID), _FALLBACK)
    template = item.reply if item else fallback
    values = _placeholder_values(_tokens_in(template), ctx, crops, prices)
    return item, _lock_loans(render(template, values), ctx)
