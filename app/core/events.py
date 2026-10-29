"""Live updates over Server-Sent Events, replacing the browser's cross-tab `storage` event.

When the console saves something farmers see (flags, content, lenders, announcements), every
open app hears "platform"; when an admin acts on one farmer (suspension, a verification, a loan
decision), that farmer's app hears "account" and reloads. Events carry no data beyond the topic,
so a client always re-reads through the API (and its permission checks).

Events travel through PostgreSQL (NOTIFY on publish, LISTEN in every worker), so with several
API workers each farmer still hears every change, whichever worker serves their stream."""

import asyncio
import json
import logging
import select
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import psycopg2
from sqlalchemy.engine import make_url

from app.config import get_settings

KEEPALIVE_SECONDS = 15
CHANNEL = "agrictec_events"

log = logging.getLogger("agrictec.events")


def _connect():
    url = make_url(get_settings().database_url)
    connection = psycopg2.connect(
        host=url.host or "localhost",
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        dbname=url.database,
        connect_timeout=5,
    )
    connection.set_session(autocommit=True)
    return connection


@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[str]
    # The farmer's ID ("" or None when signed out); admins hear everything.
    user_id: str | None
    admin: bool = field(default=False)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._publisher = None
        self._publish_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: threading.Thread | None = None

    # ---------- life cycle ----------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """At startup: deliver to this worker's streams, and LISTEN for every worker's events."""
        self._loop = loop
        self._stop.clear()
        self._listener = threading.Thread(target=self._listen, name="agrictec-events", daemon=True)
        self._listener.start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener:
            self._listener.join(timeout=3)
        with self._publish_lock:
            if self._publisher is not None:
                self._publisher.close()
                self._publisher = None

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                connection = _connect()
                with connection.cursor() as cursor:
                    cursor.execute(f"LISTEN {CHANNEL}")
                while not self._stop.is_set():
                    if select.select([connection], [], [], 1.0) == ([], [], []):
                        continue
                    connection.poll()
                    while connection.notifies:
                        note = connection.notifies.pop(0)
                        self._schedule(note.payload)
                connection.close()
            except Exception:  # The database restarted, say: reconnect shortly.
                log.warning("Live events: lost the database connection; reconnecting.", exc_info=True)
                self._stop.wait(2)

    # ---------- publishing ----------

    def publish(self, topic: str, user_id: str | None = None) -> None:
        """Sends an event to every worker. Safe from the threads that run the endpoints; a
        failure only means no live update (clients still refresh on focus)."""
        payload = json.dumps({"topic": topic, **({"userId": user_id} if user_id else {})})
        try:
            with self._publish_lock:
                if self._publisher is None or self._publisher.closed:
                    self._publisher = _connect()
                with self._publisher.cursor() as cursor:
                    cursor.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))
        except Exception:
            log.warning("Live events: could not publish %s.", topic, exc_info=True)
            with self._publish_lock:
                self._publisher = None

    def _schedule(self, payload: str) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._deliver, payload)

    def _deliver(self, payload: str) -> None:
        try:
            event = json.loads(payload)
        except ValueError:
            return
        topic, user_id = event.get("topic"), event.get("userId")
        for sub in list(self._subscribers):
            if topic == "account" and not sub.admin and sub.user_id != user_id:
                continue
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # A slow client misses an event; it reloads on its next one anyway.

    # ---------- streams ----------

    async def stream(self, *, user_id: str | None, admin: bool) -> AsyncIterator[str]:
        sub = _Subscriber(asyncio.Queue(maxsize=100), user_id, admin)
        self._subscribers.add(sub)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(sub.queue.get(), KEEPALIVE_SECONDS)
                    yield f"event: change\ndata: {message}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self._subscribers.discard(sub)


bus = EventBus()
