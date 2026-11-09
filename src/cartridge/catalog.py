"""Syncs each device's Zaparoo index into SQLite (spec 7.1).

A run reads the device first (systems, then every catalog page) and only then applies it in a single
transaction, so a socket that drops mid-page leaves the catalog exactly as it was and the run is
recorded ``failed`` with the target's sentence. Reading before the transaction also keeps the shared
connection from being held open across awaits. Every row a run touches is stamped with the run's
start time and ``mark_missing`` flips rows carrying any other stamp (no clock ordering involved).
Timestamps are UTC ISO-8601 with microseconds.

The same class also ingests each device's play history, resolves what is playing to a catalog game,
and answers the catalog queries the API serves, so every caller sees one game the same way.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from cartridge.db import Database, GameRow, HistoryRow
from cartridge.events import EventHub
from cartridge.ids import game_id
from cartridge.ratelimit import KeyedTokenBucket
from cartridge.registry import DeviceRegistry
from cartridge.targets.base import CatalogEntry, SystemAvailability, TargetError

log = logging.getLogger(__name__)

HISTORY_LIMIT = 50
SYNC_NOW_BURST = 1
SYNC_NOW_REFILL_PER_SECOND = 1 / 60


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ResolvedNowPlaying:
    title: str
    system_id: str
    game_id: str | None
    started_at: str | None


@dataclass(frozen=True)
class RecentItem:
    device: str
    game_id: str | None
    title: str
    system_id: str
    started_at: str
    play_time: int
    as_of: str | None  # the device's last history ingest, set only while that device is offline


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: Literal["not_on_device", "core_missing"] | None


@dataclass(frozen=True)
class GameItem:
    id: str
    title: str
    system_id: str
    system_name: str
    favorite: bool
    availability: dict[str, Availability]


@dataclass(frozen=True)
class GamePage:
    items: list[GameItem]
    total: int


@dataclass(frozen=True)
class SystemItem:
    id: str
    name: str
    manufacturer: str | None
    game_count: int


@dataclass(frozen=True)
class LastLaunched:
    game_id: str
    title: str
    at: str


@dataclass(frozen=True)
class SyncState:
    running: bool
    status: str | None
    finished_at: str | None
    last_good_at: str | None
    error: str | None
    seen: int
    added: int
    missing: int


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

    # -- history and now playing (spec 7.2) --------------------------------------------------

    async def ingest_history(self, device: str) -> None:
        """Cache the device's recent history. A device that can't answer is skipped, not an error."""
        target = self.registry.target(device)
        if target is None:
            raise KeyError(device)
        try:
            entries = await target.history(HISTORY_LIMIT)
        except TargetError as exc:
            log.info("History of %s not ingested: %s", device, exc)
            return
        rows = [
            HistoryRow(
                device=device,
                started_at=e.started_at,
                # ASSUMPTION Z-HISTORY-PATH: history paths equal the indexed paths
                game_id=self.db.game_id_for_path(device, e.abs_path) if e.abs_path else None,
                system_id=e.system_id,
                media_name=e.media_name,
                play_time=e.play_time,
                ingested_at=None,
            )
            for e in entries
        ]
        self.db.upsert_history(device, rows, self._stamp())

    def resolve_now_playing(self, device: str) -> ResolvedNowPlaying | None:
        if self.registry.info(device) is None:
            raise KeyError(device)
        playing = self.registry.status(device).now_playing
        if playing is None:
            return None
        gid = self.db.game_id_for_path(device, playing.abs_path) if playing.abs_path else None
        game = self.db.game(gid) if gid else None
        return ResolvedNowPlaying(
            game.title if game else playing.media_name, playing.system_id, game.id if game else None, playing.started_at
        )

    def recent(self, limit: int = 50) -> list[RecentItem]:
        as_of: dict[str, str | None] = {}
        items: list[RecentItem] = []
        for row in self.db.recent_history(limit):
            if row.device not in as_of:
                known = self.registry.info(row.device) is not None
                offline = known and not self.registry.status(row.device).online
                as_of[row.device] = self.db.history_as_of(row.device) if offline else None
            game = self.db.game(row.game_id) if row.game_id else None
            items.append(
                RecentItem(
                    row.device,
                    row.game_id,
                    game.title if game else row.media_name,
                    row.system_id,
                    row.started_at,
                    row.play_time,
                    as_of[row.device],
                )
            )
        return items

    # -- catalog queries (spec 10) -----------------------------------------------------------

    def _target_devices(self) -> list[str]:
        return [n for n in self.registry.names() if self.registry.target(n) is not None]

    def _game_items(self, rows: list[GameRow]) -> list[GameItem]:
        ids = [r.id for r in rows]
        presence = self.db.presence(ids)
        favorites = self.db.favorite_ids(ids)
        devices = self._target_devices()
        items: list[GameItem] = []
        for r in rows:
            availability: dict[str, Availability] = {}
            for d in devices:
                p = presence.get(r.id, {}).get(d)
                if p is None or not p.present:
                    availability[d] = Availability(False, "not_on_device")
                elif not p.system_available:
                    availability[d] = Availability(False, "core_missing")
                else:
                    availability[d] = Availability(True, None)
            items.append(GameItem(r.id, r.title, r.system_id, r.system_name, r.id in favorites, availability))
        return items

    def games(
        self,
        *,
        system: str | None,
        q: str | None,
        device: str | None,
        only_device: bool,
        favorites: bool,
        show_unavailable: bool,
        offset: int,
        limit: int,
    ) -> GamePage:
        if device is not None and self.registry.target(device) is None:
            raise KeyError(device)
        rows, total = self.db.query_games(
            system=system,
            q=q,
            present_on=device if only_device else None,
            favorites=favorites,
            include_unavailable=show_unavailable,
            offset=offset,
            limit=limit,
        )
        return GamePage(self._game_items(rows), total)

    def game(self, game_id: str) -> GameItem | None:
        row = self.db.game(game_id)
        return None if row is None else self._game_items([row])[0]

    def systems(self) -> list[SystemItem]:
        return [SystemItem(s.id, s.name, s.manufacturer, s.game_count) for s in self.db.systems_with_games()]

    def set_favorite(self, game_id: str, on: bool) -> bool:
        if self.db.game(game_id) is None:
            return False
        self.db.set_favorite(game_id, on, self._stamp())
        return True

    def last_launched(self, device: str) -> LastLaunched | None:
        launch = self.db.last_ok_launch(device)
        if launch is None:
            return None
        game = self.db.game(launch.game_id)
        return LastLaunched(launch.game_id, game.title if game else launch.game_id, launch.requested_at)

    def sync_running(self, device: str) -> bool:
        return device in self._running

    def sync_state(self, device: str) -> SyncState | None:
        last = self.db.last_sync(device)
        if last is None:
            return None
        good = self.db.last_good_sync(device)
        return SyncState(
            self.sync_running(device),
            last.status,
            last.finished_at,
            good.finished_at if good else None,
            last.error,
            last.seen,
            last.added,
            last.missing,
        )
