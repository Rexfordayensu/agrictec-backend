"""Phones, Ghana Cards and the verification queue (lib/profile.ts, lib/admin/card-hash.ts,
lib/admin/verification-queue.ts, lib/verification.ts)."""

import hashlib
import hmac
import re

REJECT_REASONS = ["Number format invalid", "Doesn't match name", "Duplicate", "Unclear", "Other"]
KIND_LABELS = {"phone": "phone", "location": "location", "ghanaCard": "Ghana Card"}
AUTOMATIC_REVIEWER = "Automatic check"

GHANA_PHONE = re.compile(r"^(\+233|0)[235]\d{8}$")
GHANA_CARD = re.compile(r"^GHA-\d{9}-\d$")
MASKED_CARD = re.compile(r"^GHA-\*{6}\d{3}-\d$")

# Keeps these fingerprints apart from any other keyed hash made with the same key.
_CARD_HASH_PREFIX = "agrictec:ghana-card:"


def normalize_phone(phone: str) -> str:
    value = phone.strip()
    return f"+233{value[1:]}" if value.startswith("0") else value


def mask_ghana_card(last4: str) -> str:
    """Four trailing digits including the check digit: "GHA-******789-0"."""
    return f"GHA-******{last4[:3]}-{last4[3]}" if re.fullmatch(r"\d{4}", last4 or "") else "Not verified"


def mask_full_ghana_card(card_number: str) -> str:
    return mask_ghana_card(re.sub(r"\D", "", card_number)[-4:])


def hash_ghana_card(card_number: str) -> str:
    """The stored fingerprint of a full Ghana Card number (HMAC-SHA256, hex). The number itself
    is never stored. Keyed with a server secret (GHANA_CARD_HASH_KEY, else SECRET_KEY): there are
    only about ten billion card numbers, so a plain hash could be reversed by trying them all.
    Only the server computes it, so a fingerprint can't be made up to dodge the duplicate check."""
    from app.config import get_settings

    settings = get_settings()
    key = (settings.ghana_card_hash_key or settings.secret_key).encode("utf-8")
    text = _CARD_HASH_PREFIX + card_number.strip().upper()
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def rejection_message(reason: str | None, note: str | None) -> str:
    """What a farmer sees: "Rejected: Unclear. Please resubmit." plus the reviewer's note."""
    reason = re.sub(r"[.!]+$", "", (reason or "").strip())
    note = (note or "").strip()
    base = f"Rejected: {reason}. Please resubmit." if reason else "Rejected. Please resubmit."
    return f"{base} {note}" if note else base


def parse_location(value: str) -> tuple[float, float] | None:
    """"6.7000, -1.6000" -> (6.7, -1.6)."""
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", value)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    if abs(lat) > 90 or abs(lng) > 180:
        return None
    return lat, lng
