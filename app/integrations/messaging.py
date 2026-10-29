"""SMS through Arkesel (https://developers.arkesel.com, a Ghanaian gateway) and email over SMTP.

Both are optional (ARKESEL_API_KEY; SMTP_HOST + SMTP_FROM). Each send raises MessageFailed with
a short reason, which is stored on the notification."""

import smtplib
from email.message import EmailMessage

import httpx

from app.config import get_settings
from app.domain.verification import GHANA_PHONE, normalize_phone

ARKESEL_URL = "https://sms.arkesel.com/api/v2/sms/send"
TIMEOUT_SECONDS = 15


class MessageFailed(Exception):
    pass


def msisdn(phone: str) -> str | None:
    """"0241234567" or "+233241234567" -> "233241234567" (what SMS gateways expect)."""
    value = phone.strip()
    if not GHANA_PHONE.match(value):
        return None
    return normalize_phone(value).lstrip("+")


def send_sms(phone: str, text: str, client: httpx.Client | None = None) -> None:
    settings = get_settings()
    if not settings.arkesel_api_key:
        raise MessageFailed("SMS is not configured")
    to = msisdn(phone)
    if not to:
        raise MessageFailed("no valid Ghana phone number")
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = client.post(
            ARKESEL_URL,
            headers={"api-key": settings.arkesel_api_key},
            json={"sender": settings.sms_sender_id[:11], "message": text[:459], "recipients": [to]},
        )
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise MessageFailed(f"gateway unreachable ({type(error).__name__})") from error
    finally:
        if owned:
            client.close()
    if response.status_code >= 400 or body.get("status") != "success":
        raise MessageFailed(str(body.get("message") or f"gateway answered {response.status_code}")[:150])


def send_email(to: str, subject: str, text: str) -> None:
    settings = get_settings()
    if not (settings.smtp_host and settings.smtp_from):
        raise MessageFailed("email is not configured")
    if "@" not in to or to.endswith("@agrictec.demo"):
        raise MessageFailed("no email address")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_SECONDS) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise MessageFailed(f"mail server refused ({type(error).__name__})") from error
