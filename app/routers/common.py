"""Helpers shared by the routers."""

from fastapi import Response

from app.config import get_settings
from app.core.sessions import (
    ADMIN_COOKIE,
    FARMER_COOKIE,
    ONBOARDED_COOKIE,
    ROLE_COOKIE,
    clear_cookie,
    set_cookie,
)

YEAR = 365 * 24 * 3600


def file_response(content: str, filename: str, media_type: str = "text/csv; charset=utf-8") -> Response:
    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def set_farmer_cookies(response: Response, token: str, onboarded: bool) -> None:
    set_cookie(response, FARMER_COOKIE, token, get_settings().farmer_session_days * 24 * 3600)
    set_onboarded_hint(response, onboarded)


def set_onboarded_hint(response: Response, onboarded: bool) -> None:
    """A redirect hint for proxy.ts only; the server checks the profile itself."""
    if onboarded:
        set_cookie(response, ONBOARDED_COOKIE, "1", YEAR, http_only=False)
    else:
        clear_cookie(response, ONBOARDED_COOKIE)


def clear_farmer_cookies(response: Response) -> None:
    clear_cookie(response, FARMER_COOKIE)
    clear_cookie(response, ONBOARDED_COOKIE)


def set_admin_cookies(response: Response, token: str, role: str) -> None:
    max_age = get_settings().admin_session_hours * 3600
    set_cookie(response, ADMIN_COOKIE, token, max_age)
    set_cookie(response, ROLE_COOKIE, role, max_age, http_only=False)


def clear_admin_cookies(response: Response) -> None:
    clear_cookie(response, ADMIN_COOKIE)
    clear_cookie(response, ROLE_COOKIE)
