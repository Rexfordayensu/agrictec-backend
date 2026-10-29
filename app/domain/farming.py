"""Yield prediction (lib/calculations/yield.ts) and the loan-readiness checklist
(lib/calculations/readiness.ts)."""

from app.domain.constants import YIELD_MULTIPLIERS
from app.domain.credit import is_profile_complete
from app.domain.fmt import js_round, round_int
from app.schemas.farm import FarmRecord, MomoTransaction, Profile, ReadinessItem, YieldInput
from app.schemas.platform import CropContent


def _crop(crop: str, crops: list[CropContent]) -> CropContent | None:
    return next((c for c in crops if c.crop == crop), None)


def predict_yield(data: YieldInput, crops: list[CropContent]) -> dict:
    """Deterministic: the same input always gives the same output, as a +/-10% range."""
    m = YIELD_MULTIPLIERS
    content = _crop(data.crop, crops)
    base = content.yield_per_acre if content else 0
    price = content.price_per_tonne if content else 0
    per_acre = base * m["fertilizer"][data.fertilizer] * m["irrigation"][data.irrigation] * m["season"][data.season]
    expected = per_acre * data.acres
    return {
        "low": js_round(expected * 0.9, 2),
        "expected": js_round(expected, 2),
        "high": js_round(expected * 1.1, 2),
        "per_acre": js_round(per_acre, 2),
        "revenue": round_int(expected * price),
        "price_per_tonne": price,
        "tips": list(content.tips) if content else [],
        "insight": _insight(data, js_round(per_acre, 2), base),
    }


def _insight(data: YieldInput, per_acre: float, base: float) -> str:
    """Praise strong management, or name the single biggest improvement."""
    if base == 0:
        return "We do not have baseline yield data for this crop yet. Record your harvests to build your own benchmark."
    if per_acre > base * 1.2:
        return (
            f"Excellent management! Your inputs put you about {round_int((per_acre / base - 1) * 100)}% "
            f"above the average yield for {data.crop}."
        )
    m = YIELD_MULTIPLIERS
    options = [
        (m["fertilizer"]["both"] / m["fertilizer"][data.fertilizer], "Using both organic and inorganic fertilizer"),
        (m["irrigation"]["drip"] / m["irrigation"][data.irrigation], "Adding drip irrigation"),
        (m["season"]["major"] / m["season"][data.season], "Planting in the major season (Mar–Jul)"),
    ]
    gain, advice = options[0]
    for option in options[1:]:
        if option[0] > gain:
            gain, advice = option
    if gain <= 1:
        return "Your inputs are already well optimized. Focus on timely weeding and pest control."
    return f"{advice} could raise yield by about {round_int((gain - 1) * 100)}%."


def build_readiness(
    *,
    records: list[FarmRecord],
    momo: list[MomoTransaction],
    profile: Profile | None,
    min_records: int,
    card_under_review: bool,
) -> list[ReadinessItem]:
    """The loan-readiness checklist the dashboard and the admin's farmer profile both show."""
    complete = is_profile_complete(profile)
    card_verified = bool(profile and profile.ghana_card.status == "verified")
    momo_count = len(momo)
    return [
        ReadinessItem(key="account", label="Account Created", description="Your AgriCtec account is active.", done=True),
        ReadinessItem(
            key="records",
            label="Financial Records",
            description=f"{min(len(records), min_records)} of {min_records} farm records logged.",
            done=len(records) >= min_records,
        ),
        ReadinessItem(
            key="momo",
            label="MoMo Integration",
            description=(
                f"{momo_count} MoMo transaction{'' if momo_count == 1 else 's'} recorded."
                if momo_count
                else "Log at least one MoMo transaction."
            ),
            done=momo_count >= 1,
        ),
        ReadinessItem(
            key="profile",
            label="Profile Complete",
            description=(
                "Name, phone, farm name and acreage are filled in."
                if complete
                else "Add your name, phone, farm name and acreage."
            ),
            done=complete,
        ),
        ReadinessItem(
            key="ghanaCard",
            label="Ghana Card",
            description=(
                "Your Ghana Card is verified."
                if card_verified
                else "Your Ghana Card is under review."
                if card_under_review
                else "Verify your Ghana Card in your profile."
            ),
            done=card_verified,
        ),
    ]
