"""Syncs each device's Zaparoo index into SQLite (spec 7.1).

A run reads the device first (systems, then every catalog page) and only then applies it in a single
transaction, so a socket that drops mid-page leaves the catalog exactly as it was and the run is
recorded ``failed`` with the target's sentence. Reading before the transaction also keeps the shared
connection from being held open across awaits. Every row a run touches is stamped with the run's
start time and ``mark_missing`` flips rows carrying any other stamp (no clock ordering involved).
Timestamps are UTC ISO-8601 with microseconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.ids import game_id
from cartridge.ratelimit import KeyedTokenBucket
from cartridge.registry import DeviceRegistry
from cartridge.targets.base import CatalogEntry, SystemAvailability, TargetError

log = logging.getLogger(__name__)

SYNC_NOW_BURST = 1
SYNC_NOW_REFILL_PER_SECOND = 1 / 60


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SyncResult:
    device: str
    status: Literal["ok", "failed"]
    seen: int
    added: int
    missing: int
    error: str | None


class SyncRateLimited(Exception):
    def __init__(self, wait: float) -> None:
        super().__init__(f"A sync was requested too recently; try again in {max(wait, 1):.0f}s.")
        self.wait = wait


class Catalog:
    def __init__(
        self,
        db: Database,
        registry: DeviceRegistry,
        hub: EventHub,
        *,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.registry = registry
        self.hub = hub
        self._now = now
        self._running: dict[str, asyncio.Task[SyncResult]] = {}
        self._sync_now = KeyedTokenBucket(SYNC_NOW_BURST, SYNC_NOW_REFILL_PER_SECOND, clock=clock)

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="microseconds")

    async def sync(self, device: str) -> SyncResult:
        """Sync one device, or join the run already in flight. KeyError for a device with no target."""
        if self.registry.target(device) is None:
            raise KeyError(device)
        task = self._running.get(device)
        if task is None:
            task = asyncio.create_task(self._run(device))
            self._running[device] = task
            task.add_done_callback(lambda _: self._running.pop(device, None))  # one task per device at a time
        # Shielded so a cancelled caller (a closed HTTP request) can't cancel the run others wait on.
        return await asyncio.shield(task)

    async def sync_now(self, device: str) -> SyncResult:
        """A user-requested sync: joining a run in flight is free, starting one takes a token."""
        if self.registry.target(device) is None:
            raise KeyError(device)
        if device not in self._running:
            wait = self._sync_now.take(device)
            if wait > 0:
                raise SyncRateLimited(wait)
        return await self.sync(device)

    async def _run(self, device: str) -> SyncResult:
        info = self.registry.info(device)
        target = self.registry.target(device)
        assert info is not None and target is not None
        label = info.label
        run_id: int | None = None
        seen = added = missing = 0
        error: str | None = None
        try:
            started = self._stamp()
            run_id = self.db.start_sync_run(device, started)
            self.hub.publish({"type": "sync.started", "device": device})
            systems = await target.systems()
            entries = [entry async for entry in target.catalog()]
            seen = len(entries)
            with self.db.transaction():
                added = self._apply(device, systems, entries, started)
                missing = self.db.mark_missing(device, started)
        except TargetError as exc:
            error = str(exc)
        except Exception:
            log.exception("Sync of %s failed unexpectedly.", device)
            error = f"Sync of {label} failed unexpectedly; see the server log."
        except asyncio.CancelledError:
            if run_id is not None:
                self.db.finish_sync_run(run_id, self._stamp(), "failed", 0, 0, 0, f"Sync of {label} was interrupted.")
            self.hub.publish({"type": "sync.finished", "device": device, "status": "failed"})
            raise
        status: Literal["ok", "failed"] = "ok" if error is None else "failed"
        if error is not None:
            seen = added = missing = 0
        if run_id is not None:
            try:
                self.db.finish_sync_run(run_id, self._stamp(), status, seen, added, missing, error)
            except Exception:
                log.exception("Could not record the result of the sync of %s.", device)
        self.hub.publish({"type": "sync.finished", "device": device, "status": status})
        return SyncResult(device, status, seen, added, missing, error)

    def _apply(self, device: str, systems: list[SystemAvailability], entries: list[CatalogEntry], stamp: str) -> int:
        for s in systems:
            self.db.upsert_system(s.system_id, s.name, s.manufacturer, s.release_date)
            self.db.set_device_system(device, s.system_id, s.available, s.media_count, stamp)
        added = 0
        known = {s.system_id for s in systems}
        for e in entries:
            if e.system_id not in known:  # systems() and catalog() are separate reads; the device may have changed
                self.db.ensure_system(e.system_id)
                known.add(e.system_id)
            gid = game_id(e.system_id, e.rel_path)
            if self.db.upsert_game(gid, e.system_id, e.title, stamp):
                added += 1
            self.db.upsert_device_media(device, gid, e.ref.token, e.abs_path, stamp)
        return added
