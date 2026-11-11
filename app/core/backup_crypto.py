"""Encrypted platform backups (the console's "Download backup").

A backup holds every farmer's personal data, so the file is encrypted with a passphrase the admin
chooses when downloading it: AES-256-GCM, with the key derived from the passphrase by scrypt and
a fresh salt and nonce for every file. The server never stores the passphrase, so a lost
passphrase means a backup that can't be opened, and a leaked file reveals nothing without it.
The same passphrase restores the file on any AgriCtec server (a fresh one after a disaster too).

The envelope is plain JSON:
  {"app": "agrictec", "kind": "platform-backup-encrypted", "version": 1, "exportedAt": "...",
   "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1, "salt": "<base64>"},
   "cipher": "AES-256-GCM", "nonce": "<base64>", "ciphertext": "<base64>"}"""

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.core.errors import AppError

ENCRYPTED_KIND = "platform-backup-encrypted"
ENVELOPE_VERSION = 1
PASSPHRASE_MIN_LENGTH = 12
# scrypt's cost: about 32 MB of memory and a fraction of a second per guess.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
# The envelope's own fields are authenticated with the data, so none can be swapped.
_AAD = b"agrictec:platform-backup:v1"


def passphrase_problem(passphrase: str) -> str | None:
    if len(passphrase) < PASSPHRASE_MIN_LENGTH:
        return f"Choose a backup passphrase of at least {PASSPHRASE_MIN_LENGTH} characters."
    if len(passphrase) > 1000:
        return "The backup passphrase is too long."
    return None


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def encrypt(payload: dict[str, Any], passphrase: str, exported_at: str) -> dict[str, Any]:
    problem = passphrase_problem(passphrase)
    if problem:
        raise AppError(problem)
    salt, nonce = os.urandom(16), os.urandom(12)
    plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sealed = AESGCM(_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(nonce, plain, _AAD)
    return {
        "app": "agrictec",
        "kind": ENCRYPTED_KIND,
        "version": ENVELOPE_VERSION,
        "exportedAt": exported_at,
        "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": base64.b64encode(salt).decode()},
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(sealed).decode(),
    }


def is_encrypted(document: dict[str, Any]) -> bool:
    return document.get("app") == "agrictec" and document.get("kind") == ENCRYPTED_KIND


def decrypt(envelope: dict[str, Any], passphrase: str) -> dict[str, Any]:
    """The backup inside an envelope. A wrong passphrase or a changed file both fail cleanly."""
    if envelope.get("version") != ENVELOPE_VERSION or envelope.get("cipher") != "AES-256-GCM":
        raise AppError("This encrypted backup was made by a newer version of AgriCtec.")
    if not passphrase:
        raise AppError("Enter the passphrase this backup was encrypted with.", code="passphrase_required")
    kdf = envelope.get("kdf") or {}
    try:
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
        # Bounded, so a crafted file can't make the server spend minutes deriving a key.
        # (scrypt needs 128 * n * r bytes: at most 64 MB here.)
        if kdf.get("name") != "scrypt" or not (2**14 <= n <= 2**16 and 1 <= r <= 8 and p == 1):
            raise ValueError("unsupported key settings")
        salt = base64.b64decode(kdf["salt"], validate=True)
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        sealed = base64.b64decode(envelope["ciphertext"], validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise AppError("This backup file is damaged.") from error
    try:
        plain = AESGCM(_key(passphrase, salt, n, r, p)).decrypt(nonce, sealed, _AAD)
    except InvalidTag:
        raise AppError(
            "That passphrase doesn't open this backup (or the file was changed).", code="wrong_passphrase"
        ) from None
    try:
        payload = json.loads(plain)
    except ValueError as error:
        raise AppError("This backup file is damaged.") from error
    if not isinstance(payload, dict):
        raise AppError("This backup file is damaged.")
    return payload
