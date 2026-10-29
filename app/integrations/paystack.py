"""Paystack (https://paystack.com/docs/api): payments in GHS by mobile money (MTN MoMo, Telecel
Cash, AirtelTigo Money) and card.

The flow: initialize() returns Paystack's checkout page; the farmer pays there; Paystack sends
the farmer back and also calls our webhook. A plan is activated only after verify() (or a
signed webhook) confirms the exact amount was paid. Card and wallet details never reach us."""

import hashlib
import hmac
from typing import Any

import httpx

from app.config import get_settings

BASE_URL = "https://api.paystack.co"
TIMEOUT_SECONDS = 20
CHANNELS = {"mobile-money": ["mobile_money"], "card": ["card"]}


class PaystackError(Exception):
    pass


def _headers() -> dict[str, str]:
    key = get_settings().paystack_secret_key
    if not key:
        raise PaystackError("Paystack is not configured.")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _call(method: str, path: str, client: httpx.Client | None = None, **kwargs) -> dict[str, Any]:
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = client.request(method, f"{BASE_URL}{path}", headers=_headers(), **kwargs)
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise PaystackError("Paystack could not be reached. Please try again.") from error
    finally:
        if owned:
            client.close()
    if response.status_code >= 400 or not body.get("status"):
        raise PaystackError(body.get("message") or "Paystack refused the request.")
    return body["data"]


def initialize(
    *, email: str, amount_pesewas: int, reference: str, callback_url: str, method: str, metadata: dict,
    client: httpx.Client | None = None,
) -> str:
    """Starts a payment; returns the URL of Paystack's checkout page."""
    data = _call(
        "POST",
        "/transaction/initialize",
        client,
        json={
            "email": email,
            "amount": amount_pesewas,
            "currency": "GHS",
            "reference": reference,
            "callback_url": callback_url,
            "channels": CHANNELS.get(method, ["mobile_money", "card"]),
            "metadata": metadata,
        },
    )
    return data["authorization_url"]


def verify(reference: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """The transaction as Paystack records it (status, amount in pesewas, currency, channel)."""
    return _call("GET", f"/transaction/verify/{reference}", client)


def valid_signature(raw_body: bytes, signature: str | None) -> bool:
    """A webhook is genuine when its HMAC-SHA512 (keyed with our secret key) matches."""
    key = get_settings().paystack_secret_key
    if not key or not signature:
        return False
    expected = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


def receipt_details(data: dict[str, Any]) -> dict[str, Any]:
    """What we keep from Paystack's answer: how it was paid, never a card or wallet number."""
    authorization = data.get("authorization") or {}
    return {
        "channel": data.get("channel"),
        "paidAt": data.get("paid_at") or data.get("paidAt"),
        "gatewayResponse": data.get("gateway_response"),
        "brand": authorization.get("brand"),
        "bank": authorization.get("bank"),
        "last4": authorization.get("last4"),
    }
