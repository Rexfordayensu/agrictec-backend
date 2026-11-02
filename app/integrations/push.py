"""Web push (RFC 8030): a notification straight to a phone or computer's browser, through that
browser's own push service. No account with anyone: the server signs each message with its own
VAPID key (RFC 8292), and the content is encrypted for that one device (RFC 8291, aes128gcm).

The VAPID key is derived from SECRET_KEY, so there is nothing to set up or store. Changing
SECRET_KEY changes it, and devices then need turning on again (the push service answers 403)."""

import base64
import json
import os
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

from app.config import get_settings

TIMEOUT_SECONDS = 10
# How long a push service keeps a message for a phone that is off (seconds).
TTL_SECONDS = 24 * 3600
# Order of the P-256 curve: a private key is a number from 1 to N - 1.
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@lru_cache(maxsize=4)
def _vapid_key(secret: str) -> ec.EllipticCurvePrivateKey:
    seed = HKDF(algorithm=hashes.SHA256(), length=48, salt=None, info=b"agrictec web push vapid").derive(
        secret.encode("utf-8")
    )
    return ec.derive_private_key(int.from_bytes(seed, "big") % (_P256_ORDER - 1) + 1, ec.SECP256R1())


def public_key() -> str:
    """What a browser subscribes with (applicationServerKey): the uncompressed point, base64url."""
    key = _vapid_key(get_settings().secret_key).public_key()
    return _b64url(key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """The message body for one device: aes128gcm with a fresh key and salt every time."""
    return http_ece.encrypt(
        payload,
        salt=os.urandom(16),
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=b64url_decode(p256dh),
        auth_secret=b64url_decode(auth),
        version="aes128gcm",
    )


def _authorization(endpoint: str) -> str:
    settings = get_settings()
    origin = urlsplit(endpoint)
    vapid = Vapid02(private_key=_vapid_key(settings.secret_key))
    # Who to contact about these messages: the app's own address. It must be https (or mailto:),
    # which a local http://localhost isn't.
    app_url = settings.public_app_url.rstrip("/")
    claims = {
        "aud": f"{origin.scheme}://{origin.netloc}",
        "exp": int(time.time()) + 12 * 3600,
        "sub": app_url if app_url.startswith("https://") else "mailto:admin@localhost",
    }
    return vapid.sign(claims)["Authorization"]


def send(endpoint: str, p256dh: str, auth: str, message: dict[str, Any], client: httpx.Client | None = None) -> int:
    """Sends one message to one device; the push service's status code (201 delivered, 404 or 410
    gone for good, 403 subscribed with another key), or 0 when it can't be reached."""
    body = encrypt(json.dumps(message).encode("utf-8"), p256dh, auth)
    headers = {
        "Authorization": _authorization(endpoint),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_SECONDS),
        "Urgency": "high",
    }
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        return client.post(endpoint, content=body, headers=headers).status_code
    except httpx.HTTPError:
        return 0
    finally:
        if owned:
            client.close()
