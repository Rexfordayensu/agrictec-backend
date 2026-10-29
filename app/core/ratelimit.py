"""Rate limits, kept in PostgreSQL so they hold across every API worker.

A fixed window per key (an IP address or a signed-in account): one atomic upsert per request.
Behind a proxy (the Next.js app, nginx), the client's address comes from X-Forwarded-For, which
is trusted only when the request arrives from one of TRUSTED_PROXIES (addresses or networks)."""

import ipaddress
from collections.abc import Callable
from datetime import timedelta
from functools import lru_cache

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session as DB

from app.config import get_settings
from app.core.errors import AppError
from app.db import get_db
from app.domain.clock import now_utc

_UPSERT = text(
    """
    INSERT INTO rate_limits (key, window_start, count) VALUES (:key, :now, 1)
    ON CONFLICT (key) DO UPDATE SET
        count = CASE WHEN rate_limits.window_start <= :expired THEN 1 ELSE rate_limits.count + 1 END,
        window_start = CASE WHEN rate_limits.window_start <= :expired THEN :now ELSE rate_limits.window_start END
    RETURNING count
    """
)


@lru_cache(maxsize=8)
def _networks(entries: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries)


def _trusted(peer: str) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in _networks(tuple(get_settings().trusted_proxies)))


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _trusted(peer):
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer


def hit(db: DB, key: str, limit: int, window: timedelta) -> bool:
    """Counts one request; False once `limit` is passed within the window. Committed at once, so
    a refused request still counts and a failing one can't undo it."""
    now = now_utc()
    count = db.execute(_UPSERT, {"key": key, "now": now, "expired": now - window}).scalar_one()
    db.commit()
    return count <= limit


def rate_limit(name: str, limit: int, per_seconds: int) -> Callable[..., None]:
    """A dependency: at most `limit` requests per `per_seconds` from one client address."""
    window = timedelta(seconds=per_seconds)

    def dependency(request: Request, db: DB = Depends(get_db)) -> None:
        if not get_settings().rate_limits_enabled:
            return
        if not hit(db, f"{name}:{client_ip(request)}", limit, window):
            raise AppError(
                "Too many requests. Please wait a moment and try again.",
                code="rate_limited",
                status_code=429,
            )

    return dependency
