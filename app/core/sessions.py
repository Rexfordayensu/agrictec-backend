"""Server-side sessions behind httpOnly cookies.

The cookie names are the ones the frontend's proxy.ts already checks, so its redirects keep
working: `agrictec_session` (farmer) and `agrictec_admin` (admin). Unlike the demo's flags, these hold an
opaque token that the server checks on every request."""

from datetime import datetime, timedelta

from fastapi import Request, Response
from sqlalchemy import update
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.security import new_token, token_digest
from app.domain.clock import now_utc
from app.models import AuthSession

FARMER_COOKIE = "agrictec_session"
ADMIN_COOKIE = "agrictec_admin"
GUEST_COOKIE = "agrictec_guest"
# The read-only farmer view is per tab, like the demo's sessionStorage: the viewing tab sends its
# token in this header (it keeps setting its own `agrictec_impersonating` hint for proxy.ts).
IMPERSONATION_HEADER = "x-impersonation-token"
# Hints for proxy.ts redirects only (readable by the page; they grant nothing).
ROLE_COOKIE = "agrictec_role"
ONBOARDED_COOKIE = "agrictec_onboarded"

IMPERSONATION_HOURS = 2


def create_session(
    db: DB, kind: str, *, user_id: str | None = None, admin_id: str | None = None, ttl: timedelta
) -> tuple[str, AuthSession]:
    token = new_token()
    now = now_utc()
    record = AuthSession(
        id=token_digest(token),
        kind=kind,
        user_id=user_id,
        admin_id=admin_id,
        created_at=now,
        last_activity_at=now,
        expires_at=now + ttl,
    )
    db.add(record)
    return token, record


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def find_session(db: DB, token: str | None, kinds: tuple[str, ...], now: datetime) -> AuthSession | None:
    """The live session for a token (not ended, not expired), or None."""
    if not token:
        return None
    record = db.get(AuthSession, token_digest(token))
    if not record or record.kind not in kinds or record.ended_at or record.expires_at <= now:
        return None
    return record


def end_session(record: AuthSession, now: datetime | None = None) -> None:
    record.ended_at = now or now_utc()


def end_sessions_for(db: DB, *, user_id: str | None = None, admin_id: str | None = None) -> None:
    """Signs a farmer (or an admin) out everywhere, e.g. after a suspension or a password reset."""
    query = update(AuthSession).where(AuthSession.ended_at.is_(None))
    if user_id:
        query = query.where(AuthSession.user_id == user_id)
    if admin_id:
        query = query.where(AuthSession.admin_id == admin_id)
    db.execute(query.values(ended_at=now_utc()))


def set_cookie(response: Response, name: str, value: str, max_age: int, *, http_only: bool = True) -> None:
    settings = get_settings()
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=http_only,
        secure=settings.cookie_secure,
        samesite="lax",
        domain=settings.cookie_domain,
        path="/",
    )


def clear_cookie(response: Response, name: str) -> None:
    settings = get_settings()
    response.delete_cookie(name, path="/", domain=settings.cookie_domain)
