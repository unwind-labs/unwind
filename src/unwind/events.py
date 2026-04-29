"""Typed event envelopes + a per-slug broadcast hub.

The watcher pushes events into :class:`EventBus`. Each WebSocket connection
gets its own asyncio queue subscribed to one slug; messages fan out from the
bus onto all subscribers with a bounded buffer (slow clients drop events
rather than stall the server).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


log = logging.getLogger("unwind.events")


EventType = Literal[
    "session_created",
    "session_updated",
    "messages_appended",
    "tree_changed",
]


@dataclass
class Event:
    type: EventType
    slug: str
    session_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "slug": self.slug,
            "session_id": self.session_id,
            **self.payload,
        }


class EventBus:
    """In-process pub/sub keyed by slug.

    Thread-safe for publish from worker threads (watchdog fires events on its
    own thread); subscribe/consume runs on the asyncio event loop.
    """

    def __init__(self, max_queue: int = 256) -> None:
        self._max = max_queue
        self._subs: dict[str, set[asyncio.Queue[Event]]] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def subscribe(self, slug: str) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max)
        async with self._lock:
            self._subs.setdefault(slug, set()).add(q)
        return q

    async def unsubscribe(self, slug: str, q: asyncio.Queue[Event]) -> None:
        async with self._lock:
            subs = self._subs.get(slug)
            if subs is None:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(slug, None)

    def publish_threadsafe(self, event: Event) -> None:
        """Schedule a publish from any thread."""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._publish_now, event)
        except RuntimeError:
            # Loop already shut down.
            pass

    def _publish_now(self, event: Event) -> None:
        subs = self._subs.get(event.slug)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.debug("dropping event for slow subscriber slug=%s", event.slug)


_bus: Optional[EventBus] = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
