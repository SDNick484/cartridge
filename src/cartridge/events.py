"""In-process fan-out of server events to SSE clients (spec 7.1, 9.6).

``publish`` never blocks: each subscriber has a bounded queue and a slow one loses its oldest event,
so one stalled browser can't hold up a sync or the others. LAN and remote clients are capped
separately (``max_clients`` each) so a flood on one side can't starve the other.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

Event = dict[str, object]
Pool = Literal["lan", "remote"]

QUEUE_SIZE = 100


class TooManyClients(Exception):
    """The pool is full; the web layer maps this to 503 ``too_many_clients``."""


class EventHub:
    def __init__(self, max_clients: int = 50) -> None:
        self.max_clients = max_clients
        self._subs: dict[Pool, set[asyncio.Queue[Event | None]]] = {"lan": set(), "remote": set()}
        self._closed = False

    def client_count(self, pool: Pool) -> int:
        return len(self._subs[pool])

    def publish(self, event: Event) -> None:
        if self._closed:  # a late event must not evict the end-of-stream marker
            return
        for queue in self._subs["lan"] | self._subs["remote"]:
            self._offer(queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue[Event | None], item: Event | None) -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(item)

    def close(self) -> None:
        """End every open subscription (and any made later) once its queued events are read."""
        self._closed = True
        for queue in self._subs["lan"] | self._subs["remote"]:
            self._offer(queue, None)

    @asynccontextmanager
    async def subscribe(self, pool: Pool) -> AsyncIterator[AsyncIterator[Event]]:
        subs = self._subs[pool]
        if len(subs) >= self.max_clients:
            raise TooManyClients(f"Too many {pool} clients are connected; close another tab and try again.")
        queue: asyncio.Queue[Event | None] = asyncio.Queue(QUEUE_SIZE)
        if self._closed:
            queue.put_nowait(None)
        subs.add(queue)

        async def events() -> AsyncIterator[Event]:
            while (item := await queue.get()) is not None:
                yield item

        try:
            yield events()
        finally:
            subs.discard(queue)
