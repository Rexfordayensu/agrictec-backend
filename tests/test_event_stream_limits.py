"""The live-update stream can't be used to tie up the server: open streams are capped per visitor
and per worker, a refused one tells the browser when to retry, and closing frees the slot."""

import asyncio

from app.core import events
from app.core.events import EventBus


def test_streams_are_capped_per_visitor_and_freed_when_closed(monkeypatch):
    monkeypatch.setattr(events, "MAX_STREAMS_PER_CLIENT", 2)
    bus = EventBus()

    async def scenario():
        first = bus.stream(user_id=None, admin=False, client="1.2.3.4")
        second = bus.stream(user_id=None, admin=False, client="1.2.3.4")
        assert await first.__anext__() == ": connected\n\n"
        assert await second.__anext__() == ": connected\n\n"
        assert bus.open_streams("1.2.3.4") == 2

        third = bus.stream(user_id=None, admin=False, client="1.2.3.4")
        refused = await third.__anext__()
        assert refused.startswith("retry: ") and ": busy" in refused
        # Another visitor is unaffected.
        other = bus.stream(user_id=None, admin=False, client="5.6.7.8")
        assert await other.__anext__() == ": connected\n\n"

        await first.aclose()
        assert bus.open_streams("1.2.3.4") == 1
        again = bus.stream(user_id=None, admin=False, client="1.2.3.4")
        assert await again.__anext__() == ": connected\n\n"
        for stream in (second, other, again):
            await stream.aclose()
        assert bus.open_streams() == 0 and bus.open_streams("1.2.3.4") == 0

    asyncio.run(scenario())


def test_the_worker_wide_cap_refuses_new_streams(monkeypatch):
    monkeypatch.setattr(events, "MAX_STREAMS", 1)
    bus = EventBus()

    async def scenario():
        first = bus.stream(user_id=None, admin=False, client="a")
        assert await first.__anext__() == ": connected\n\n"
        assert (await bus.stream(user_id=None, admin=False, client="b").__anext__()).startswith("retry: ")
        await first.aclose()

    asyncio.run(scenario())
