"""ZaparooTarget: the LaunchTarget for a MiSTer running Zaparoo Core, on top of ZaparooClient.

Notes on behaviour worth knowing:
- ``media.started`` may arrive before or after the ``run`` reply (not guaranteed), so now-playing is
  driven only by notifications and ``media.active``, never by the reply. A generation counter stops a
  slow ``media.active`` read from overwriting a newer notification.
- A game's ``rel_path`` comes from Core's ``relativePath``, or from the absolute path when the device
  config says its share is mounted elsewhere (``strip_prefixes``). ``launch`` recomputes it from
  ``media.meta`` and compares, so a ref from before a reindex is caught as stale.
- Zaparoo errors during ``catalog``/``history``/``systems`` propagate: a half-read catalog must fail the sync.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from ..config import DeviceConfig
from ..ids import normalize_rel_path, rel_path_from_abs
from ..tasks import stop_task
from ..zaparoo_client import (
    RUN_TIMEOUT,
    EncryptionRequired,
    ZaparooClient,
    ZaparooError,
    ZaparooTimeout,
    ZaparooUnavailable,
)
from .base import (
    Blocked,
    CatalogEntry,
    DeviceBusy,
    DeviceState,
    DeviceStatus,
    EventKind,
    Gone,
    HistoryEntry,
    Image,
    LaunchError,
    LaunchFailed,
    LaunchTarget,
    LaunchTimeout,
    NowPlaying,
    Offline,
    StaleRef,
    SystemAvailability,
    TargetError,
    TargetEvent,
    TargetRef,
)

log = logging.getLogger(__name__)

SETUP_HINT = 'See the README section "Install Zaparoo Core on a MiSTer".'
SEARCH_PAGE = 500
META_BATCH = 100
STOP_WAIT = 5.0  # ASSUMPTION Z-STOP-MENU: stop is followed by media.stopped (or an empty media.active) this fast
STOP_POLL = 0.5


class ZaparooTarget:
    platform: Literal["mister", "batocera"]

    def __init__(self, config: DeviceConfig, client: ZaparooClient | None = None) -> None:
        self.name = config.name
        self.model = config.model
        self.platform = config.platform
        self._label = config.label
        self._strip = config.strip_prefixes
        self._client = client or ZaparooClient(config.label, config.host, config.port, setup_hint=SETUP_HINT)
        self._listeners: list[Callable[[TargetEvent], None]] = []
        self._now_playing: NowPlaying | None = None
        self._gen = 0  # bumped whenever now-playing may have changed, to discard stale media.active reads
        self._stops = 0  # media.stopped notifications seen
        self._tasks: set[asyncio.Task[None]] = set()
        self._client.add_listener(self._on_notification)
        self._client.add_state_listener(self._on_state)

    # --- lifecycle and status --------------------------------------------------------------------
    async def start(self) -> None:
        await self._client.start()

    async def close(self) -> None:
        for task in list(self._tasks):
            await stop_task(task, 1.0, f"Zaparoo refresh for {self._label}")
        await self._client.stop()

    def status(self) -> DeviceStatus:
        c = self._client
        state: DeviceState
        message: str | None
        if not c.tried.is_set():
            state, message = "connecting", f"Still connecting to {self._label}."
        elif c.needs_pairing:
            state, message = "needs_pairing", self._pairing_message()
        elif c.connected:
            state, message = "online", None
        else:
            state = "offline"
            message = c.error or f"No answer from {self._label}: is it on and on the network?"
        return DeviceStatus(state, message, c.version, self._now_playing if state == "online" else None)

    def _pairing_message(self) -> str:
        return (
            f"{self._label} needs pairing: Zaparoo's encryption is on and Cartridge can't pair yet. "
            "Turn encryption off in Zaparoo's config (see the README)."
        )

    def _unreachable(self) -> str:
        if self._client.needs_pairing:  # the client dropped the socket, so its own error text has no label
            return self._pairing_message()
        return self._client.error or f"No answer from {self._label}: is it on and on the network?"

    async def _read(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """A client call for the read methods: Zaparoo's exceptions become TargetError sentences."""
        label = self._label
        try:
            return await self._client.call(method, params)
        except EncryptionRequired as exc:
            raise TargetError(self._pairing_message()) from exc
        except ZaparooUnavailable as exc:
            raise TargetError(self._unreachable()) from exc
        except ZaparooTimeout as exc:
            raise TargetError(f"{label} didn't answer in time; try again in a moment.") from exc
        except ZaparooError as exc:
            log.warning("%s: %s failed: %s", self.name, method, exc)
            raise TargetError(f"{label} couldn't answer a request ({method}); the server log has details.") from exc

    def subscribe(self, listener: Callable[[TargetEvent], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, kind: EventKind) -> None:
        for listener in list(self._listeners):
            try:
                listener(TargetEvent(self.name, kind))
            except Exception:  # a subscriber's bug must not break the device
                log.exception("%s: event listener failed on %s", self.name, kind)

    def _on_state(self, up: bool) -> None:
        self._gen += 1
        if up:
            task = asyncio.create_task(self._refresh_active(), name=f"zaparoo active {self.name}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            self._now_playing = None
            self._emit("status")

    async def _refresh_active(self) -> None:
        gen = self._gen
        try:
            active = await self._client.call("media.active")
        except (ZaparooError, ZaparooTimeout, ZaparooUnavailable) as exc:
            log.warning("%s: couldn't read the active game: %s", self.name, exc)
            active = None
        if gen == self._gen:  # a notification since means this read is stale
            self._now_playing = _now_playing(active)
        self._emit("status")

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "media.started":
            self._gen += 1
            self._now_playing = _now_playing(params)
            self._emit("media.started")
        elif method == "media.stopped":
            self._gen += 1
            self._stops += 1
            self._now_playing = None
            self._emit("media.stopped")
        elif method == "media.indexing" and params.get("indexing") is False:  # ASSUMPTION Z-INDEXING
            self._emit("indexing.finished")

    # --- reading the library ---------------------------------------------------------------------
    async def systems(self) -> list[SystemAvailability]:
        systems = (await self._read("systems", {"all": True})).get("systems") or []
        launchers = (await self._read("launchers")).get("launchers") or []
        runnable = {str(ln.get("systemId", "")).lower() for ln in launchers if ln.get("available")}
        return [
            SystemAvailability(
                system_id=s["id"],
                name=s.get("name") or s["id"],
                manufacturer=s.get("manufacturer") or None,
                release_date=s.get("releaseDate") or None,
                available=str(s["id"]).lower() in runnable,
                media_count=int(s.get("mediaCount") or 0),
            )
            for s in systems
        ]

    def _rel_path(self, abs_path: str, relative: object) -> str | None:
        # Configured prefixes override relativePath outright: mixing the two would split one game's id (spec 6.2).
        if self._strip:
            return rel_path_from_abs(abs_path, self._strip) or None
        if isinstance(relative, str) and relative:
            return normalize_rel_path(relative) or None
        return None

    async def catalog(self) -> AsyncIterator[CatalogEntry]:
        systems = (await self._read("systems", {"all": True})).get("systems") or []
        for system in systems:
            if int(system.get("mediaCount") or 0) <= 0:
                continue
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"systems": [system["id"]], "maxResults": SEARCH_PAGE}
                if cursor is not None:
                    params["cursor"] = cursor
                result = await self._read("media.search", params)
                rows = [r for r in result.get("results") or [] if isinstance(r, dict) and "mediaId" in r]
                relatives = await self._relative_paths(rows)
                for row in rows:
                    abs_path = str(row.get("path") or "")
                    rel = self._rel_path(abs_path, relatives.get(row["mediaId"]))
                    if rel is None:
                        log.warning(
                            "%s: skipping media %s: no usable relative path (check strip_prefixes)",
                            self._label,
                            row["mediaId"],
                        )
                        continue
                    # ASSUMPTION Z-SEARCH-FIELDS: the system is `systemId` or `system.id`
                    sys_id = row.get("systemId") or (row.get("system") or {}).get("id") or system["id"]
                    yield CatalogEntry(
                        system_id=str(sys_id),
                        title=str(row.get("name") or rel),
                        rel_path=rel,
                        abs_path=abs_path,
                        ref=TargetRef(str(row["mediaId"]), rel),
                    )
                cursor = (result.get("pagination") or {}).get("nextCursor")
                if not cursor:
                    break

    async def _relative_paths(self, rows: list[dict[str, Any]]) -> dict[Any, str | None]:
        """relativePath per mediaId: from the search row, or media.meta in batches for rows lacking it.

        With strip_prefixes the path comes from the absolute path, so no lookup is needed.
        """
        found: dict[Any, str | None] = {r["mediaId"]: r.get("relativePath") for r in rows}
        need = [] if self._strip else [mid for mid, rel in found.items() if not rel]
        for i in range(0, len(need), META_BATCH):
            batch = need[i : i + META_BATCH]
            result = await self._read("media.meta", {"items": [{"mediaId": m} for m in batch]})
            for item in result.get("items") or []:
                media = item.get("media") if isinstance(item, dict) else None
                if isinstance(media, dict):
                    found[item.get("mediaId")] = media.get("relativePath")
        return found

    async def artwork(self, ref: TargetRef, max_size: int) -> Image | None:
        try:
            result = await self._client.call(
                "media.image",
                {
                    "mediaId": int(ref.token),
                    "imageTypes": ["boxart", "cover"],
                    "maxSize": max_size,
                    "delivery": "inline",
                },
            )
        except (ValueError, ZaparooError, ZaparooTimeout, ZaparooUnavailable):
            return None
        # ASSUMPTION Z-IMAGE: no art comes back as an error or as empty data
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, str) or not data:
            return None
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
        return Image(raw, str(result.get("contentType") or "image/webp")) if raw else None

    async def history(self, limit: int) -> list[HistoryEntry]:
        result = await self._read("media.history", {"limit": limit, "distinctMedia": True})
        return [
            HistoryEntry(
                system_id=str(e.get("systemId") or ""),
                media_name=str(e.get("mediaName") or ""),
                abs_path=e.get("mediaPath") or None,
                started_at=str(e["startedAt"]),
                play_time=int(e.get("playTime") or 0),
            )
            for e in result.get("entries") or []
            if isinstance(e, dict) and e.get("startedAt")
        ]

    # --- launching -------------------------------------------------------------------------------
    async def launch(self, ref: TargetRef, *, replace: bool = False) -> None:
        label = self._label
        stale = StaleRef(f"{label}'s game list has changed since the last sync; sync again.")
        try:
            media_id = int(ref.token)
        except ValueError:
            raise stale from None
        try:
            media = (await self._client.call("media.meta", {"mediaId": media_id})).get("media") or {}
        except EncryptionRequired:
            raise Offline(self._pairing_message()) from None
        except ZaparooError:
            raise stale from None  # Core answered, and doesn't know this id
        except ZaparooTimeout:
            raise LaunchTimeout(f"{label} didn't answer in time; try again.") from None
        except ZaparooUnavailable:
            raise self._offline() from None
        if self._rel_path(str(media.get("path") or ""), media.get("relativePath")) != ref.rel_path:
            raise stale
        if media.get("isMissing"):
            raise Gone(f"The file is gone from {label}; sync again.")
        zap_script = media.get("zapScript")
        if not isinstance(zap_script, str) or not zap_script:
            raise LaunchFailed(
                f"{label} has no way to start this game; sync again, and reindex on the device if it persists."
            )
        if replace and self._now_playing is not None:
            await self.return_to_menu()
        try:
            # ASSUMPTION Z-RUN-WAITS: the reply comes after the launch finished (Core waits up to 30 s)
            await self._client.call("run", {"text": zap_script}, timeout=RUN_TIMEOUT)
        except ZaparooError as exc:
            raise self._run_failure(exc) from None
        except ZaparooTimeout:
            raise LaunchTimeout(f"{label} didn't confirm the launch in time; it may still be starting.") from None
        except ZaparooUnavailable:
            raise self._offline() from None

    def _offline(self) -> Offline:
        if self._client.needs_pairing:
            return Offline(self._pairing_message())
        return Offline(f"{self._label} isn't reachable right now: is it on and on the network?")

    def _run_failure(self, exc: ZaparooError) -> LaunchError:
        label = self._label
        match exc.category:
            case "media_not_found":
                return Gone(f"The file is gone from {label}; sync again.")
            case "busy":
                return DeviceBusy(f"{label} is busy starting something else; wait a moment and try again.")
            case "blocked":
                return Blocked(f"{label} won't launch this right now (a Zaparoo play limit or rule blocked it).")
            case "timeout":
                return LaunchTimeout(f"{label} took too long to launch this; it may still be starting.")
            case "launch_repair":
                return LaunchFailed(
                    f"{label} couldn't start this game ({exc.reason or 'its launcher needs repair'}); "
                    "check that the core is installed.",
                    exc.reason,
                )
        log.warning("%s: run failed: %s", self.name, exc)
        return LaunchFailed(f"{label} couldn't start this game; the server log has details.", exc.reason)

    async def return_to_menu(self) -> None:
        label = self._label
        stops = self._stops
        try:
            await self._client.call("stop")
            async with asyncio.timeout(STOP_WAIT):  # ASSUMPTION Z-STOP-MENU
                while self._stops == stops:
                    if await self._client.call("media.active") is None:
                        self._now_playing = None
                        return
                    await asyncio.sleep(STOP_POLL)
        except TimeoutError:
            raise LaunchTimeout(f"{label} didn't return to its menu in time; try again.") from None
        except ZaparooTimeout:
            raise LaunchTimeout(f"{label} didn't answer in time; try again.") from None
        except ZaparooUnavailable:
            raise self._offline() from None
        except ZaparooError:
            raise LaunchFailed(f"{label} couldn't return to its menu; the server log has details.") from None


def _now_playing(active: object) -> NowPlaying | None:
    if not isinstance(active, dict) or not active:
        return None
    return NowPlaying(
        system_id=str(active.get("systemId") or ""),
        media_name=str(active.get("mediaName") or ""),
        abs_path=active.get("mediaPath") or None,
        started_at=active.get("started") or active.get("startedAt") or None,
    )


_conforms: type[LaunchTarget] = ZaparooTarget  # mypy checks the Protocol here
