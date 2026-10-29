"""Ghana Card checks against the NIA register through Smile ID
(https://docs.usesmileid.com, ID verification over REST).

The answer is advice for the admin who reviews the card ("number verified, names match"); a
person still approves or rejects. The full number is sent to Smile ID once, over HTTPS, and is
never stored here: only the outcome and the name match level are kept.

Built against Smile ID's documented REST contract and tested with recorded answers; confirm the
ID type and result codes with your Smile ID account (sandbox first) before going live."""

import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings

SANDBOX_URL = "https://testapi.smileidentity.com/v1/id_verification"
PRODUCTION_URL = "https://api.smileidentity.com/v1/id_verification"
ID_TYPE = "GHANA_CARD"
TIMEOUT_SECONDS = 20

# Smile ID result codes, as documented.
RESULTS = {
    "1012": "verified",       # ID number validated
    "1013": "not_found",      # ID number not found
    "1014": "unsupported",    # unsupported ID type / country
    "1015": "unavailable",    # the ID authority is unavailable
    "1016": "unavailable",    # need to activate the product
}


def signature(api_key: str, partner_id: str, timestamp: str) -> str:
    """HMAC-SHA256 of timestamp + partner ID + "sid_request", base64 (Smile ID's scheme)."""
    message = f"{timestamp}{partner_id}sid_request".encode("utf-8")
    return base64.b64encode(hmac.new(api_key.encode("utf-8"), message, hashlib.sha256).digest()).decode("ascii")


def check_ghana_card(card_number: str, full_name: str, user_id: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """The outcome for the reviewer: {provider, result, names, checkedAt}. Never raises: an
    unreachable service is reported as "unavailable"."""
    settings = get_settings()
    checked_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    outcome: dict[str, Any] = {"provider": "Smile ID", "checkedAt": checked_at}
    if not (settings.smile_id_partner_id and settings.smile_id_api_key):
        return {**outcome, "result": "not_configured"}
    first, _, last = full_name.strip().partition(" ")
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = {
        "source_sdk": "rest_api",
        "source_sdk_version": "1.0.0",
        "partner_id": settings.smile_id_partner_id,
        "timestamp": timestamp,
        "signature": signature(settings.smile_id_api_key, settings.smile_id_partner_id, timestamp),
        "country": "GH",
        "id_type": ID_TYPE,
        "id_number": card_number.strip().upper(),
        **({"first_name": first} if first else {}),
        **({"last_name": last.strip()} if last.strip() else {}),
        "partner_params": {"job_id": str(uuid.uuid4()), "user_id": user_id, "job_type": 5},
    }
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = client.post(SANDBOX_URL if settings.smile_id_sandbox else PRODUCTION_URL, json=body)
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return {**outcome, "result": "unavailable"}
    finally:
        if owned:
            client.close()
    code = str(data.get("ResultCode", ""))
    actions = data.get("Actions") or {}
    return {
        **outcome,
        "result": RESULTS.get(code, "error"),
        "resultCode": code or None,
        "resultText": data.get("ResultText"),
        # "Exact Match", "Partial Match", "No Match", "Not Provided"...
        "names": actions.get("Names"),
    }
