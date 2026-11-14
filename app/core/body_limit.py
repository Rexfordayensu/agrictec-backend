"""A ceiling on request size, applied before a request is read.

Without it a client can send a body of any size: an upload is received in full (and spooled to
disk) before an endpoint's own 5 or 10 MB check can refuse it. Requests above the ceiling get 413
straight away, whether they declare their length (Content-Length) or stream it (chunked)."""

import json


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Pure ASGI middleware: `default` bytes for every request, `overrides` for paths ending in a
    given suffix (the admin's full-platform backup and restore)."""

    def __init__(self, app, *, default: int, overrides: dict[str, int] | None = None):
        self.app = app
        self.default = default
        self.overrides = overrides or {}

    def _limit(self, path: str) -> int:
        for suffix, limit in self.overrides.items():
            if path.endswith(suffix):
                return limit
        return self.default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope.get("path", ""))
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                size = limit + 1
            if size > limit:
                await _refuse(send, limit)
                return

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await _refuse(send, limit)


async def _refuse(send, limit: int) -> None:
    body = json.dumps({
        "detail": f"This request is too large (the limit is {limit // (1024 * 1024)} MB).",
        "code": "request_too_large",
    }).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
