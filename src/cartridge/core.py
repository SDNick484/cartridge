"""The composed server core and the background work that keeps it fresh (spec 4, 7.1, 7.2, 8).

``build_core`` wires the pieces and starts the device connections; ``Background`` reacts to them:
the startup sync, the periodic one, syncs after indexing, history ingest, art prefetch, and the
fan-out of device events to the SSE hub. ``close_core`` tears everything down in an order that lets
a sync cut off mid-run still write its "interrupted" row.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cartridge import __version__
from cartridge.art import ArtCache
from cartridge.catalog import Catalog
from cartridge.config import Devices, Settings
from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.launch import LaunchService
from cartridge.registry import DeviceRegistry, TargetFactory
from cartridge.targets.base import TargetEvent
from cartridge.targets.zaparoo import ZaparooTarget
from cartridge.tasks import spawn_task, stop_tasks

log = logging.getLogger(__name__)

STOP_TIMEOUT = 5.0
PREFETCH_PAGE = 500


@dataclass
class Core:
    settings: Settings
    db: Database
    registry: DeviceRegistry
    catalog: Catalog
    art: ArtCache
    hub: EventHub
    launcher: LaunchService
    http: httpx.AsyncClient | None = None
    background: Background | None = None


def database_path(settings: Settings) -> Path:
    return settings.data_dir / "cartridge.db"


def open_database(settings: Settings) -> Database:
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Database.open(database_path(settings))


async def build_core(
    settings: Settings,
    devices: Devices,
    *,
    http: httpx.AsyncClient | None = None,
    factory: TargetFactory = ZaparooTarget,
) -> Core:
    db = open_database(settings)
    owned = http is None
    client = http or httpx.AsyncClient(
        headers={"User-Agent": f"cartridge/{__version__}"}, follow_redirects=False, timeout=10
    )
    try:
        hub = EventHub()
        registry = DeviceRegistry(devices, factory)
        catalog = Catalog(db, registry, hub)
        art = ArtCache(
            db,
            registry,
            catalog,
            settings.data_dir / "art",
            http=client,
            online=settings.art_online,
            version=__version__,
        )
        core = Core(settings, db, registry, catalog, art, hub, LaunchService(catalog, registry, db, hub), http=client)
        await registry.start()
    except BaseException:
        db.close()
        if owned:
            await client.aclose()
        raise
    return core


class Background:
    """Owns every long-running task of the server. ``start`` after ``build_core``, ``stop`` before closing the core."""

    def __init__(
        self,
        core: Core,
        *,
        interval: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.core = core
        self.interval = interval if interval is not None else core.settings.sync_interval_hours * 3600
        self._sleep = sleep
        self._tasks: set[asyncio.Task[None]] = set()
        self._online: set[str] = set()
        self._seen: set[str] = set()
        self._stopped = False

    def start(self) -> None:
        self.core.background = self
        self.core.registry.subscribe(self._on_event)
        self._spawn(self._periodic(), "periodic sync")
        for name in self.core.registry.names():
            self._observe(name)  # a device may already be online when we start listening

    async def stop(self) -> None:
        self._stopped = True
        await stop_tasks(list(self._tasks), STOP_TIMEOUT, "Cartridge background tasks")
        await self.core.catalog.cancel_runs()  # syncs are shielded from their callers, so cancel them directly

    def _spawn(self, coro: Coroutine[Any, Any, None], what: str) -> None:
        task = spawn_task(self._guard(coro, what), name=f"cartridge: {what}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guard(coro: Coroutine[Any, Any, None], what: str) -> None:
        try:
            await coro
        except Exception:
            log.exception("Background %s failed.", what)

    def _on_event(self, event: TargetEvent) -> None:
        if self._stopped:
            return
        self.core.hub.publish({"type": "device", "device": event.device})
        if event.kind == "status":
            self._observe(event.device)
        elif event.kind == "media.stopped":
            self._spawn(self._ingest(event.device), f"history of {event.device}")
        elif event.kind == "indexing.finished":
            self._spawn(self._resync(event.device), f"sync of {event.device}")

    def _observe(self, name: str) -> None:
        """First time online: sync, then history. Each later return: history only."""
        if self.core.registry.target(name) is None:
            return
        if not self.core.registry.status(name).online:
            self._online.discard(name)
            return
        if name in self._online:
            return
        self._online.add(name)
        if name in self._seen:
            self._spawn(self._ingest(name), f"history of {name}")
        else:
            self._spawn(self._first_online(name), f"startup sync of {name}")

    async def _first_online(self, name: str) -> None:
        ok = await self._sync(name)
        if ok:
            self._seen.add(name)  # a failed first sync is retried on the next online transition
        await self._ingest(name)
        if ok:
            await self._prefetch(name)

    async def _resync(self, name: str) -> None:
        if await self._sync(name):
            await self._prefetch(name)

    async def _sync(self, name: str) -> bool:
        return (await self.core.catalog.sync(name)).status == "ok"

    async def _ingest(self, name: str) -> None:
        await self.core.catalog.ingest_history(name)
        self.core.hub.publish({"type": "device", "device": name})  # an open Recently played view refreshes on it

    async def _prefetch(self, name: str) -> None:
        ids: list[str] = []
        while True:
            rows, total = self.core.db.query_games(
                system=None,
                q=None,
                present_on=name,
                favorites=False,
                include_unavailable=True,
                offset=len(ids),
                limit=PREFETCH_PAGE,
            )
            ids.extend(r.id for r in rows)
            if not rows or len(ids) >= total:
                break
        await self.core.art.prefetch(ids)

    async def _periodic(self) -> None:
        while True:
            await self._sleep(self.interval)
            for name in self.core.registry.names():
                if self.core.registry.target(name) is not None and self.core.registry.status(name).online:
                    self._spawn(self._resync(name), f"periodic sync of {name}")


async def close_core(core: Core) -> None:
    """End streams first, stop background work (a cancelled sync writes to the DB), then close what it used."""
    core.hub.close()
    if core.background is not None:
        await core.background.stop()
    await core.art.close()
    await core.registry.close()
    core.db.close()
    if core.http is not None:
        await core.http.aclose()
