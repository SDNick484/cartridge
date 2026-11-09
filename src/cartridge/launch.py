"""The launch and return-to-menu flow (spec 7.3).

Every request is validated in the spec's fixed order and nothing reaches a device until all checks
pass; a rejection never touches the device. A token taken at the rate-limit check is refunded when a
later check rejects the request or recognises a double tap, since nothing was sent. Only requests
that passed validation write a ``launches`` row. Messages are sentences naming the device by label
and the game by title; they never carry a host, path or target ref.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from cartridge.catalog import Catalog, utcnow
from cartridge.db import Database, DeviceMedia
from cartridge.errors import ApiError
from cartridge.events import EventHub
from cartridge.ids import game_id, is_game_id, normalize_rel_path
from cartridge.ratelimit import refusal
from cartridge.registry import DeviceRegistry
from cartridge.targets.base import LaunchError, LaunchFailed, StaleRef, TargetRef

log = logging.getLogger(__name__)

IDEMPOTENCY_TTL = 60.0


@dataclass(frozen=True)
class LaunchRequest:
    device: str
    game: str
    replace: bool
    request_id: str


class LaunchService:
    def __init__(
        self,
        catalog: Catalog,
        registry: DeviceRegistry,
        db: Database,
        hub: EventHub,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.catalog = catalog
        self.registry = registry
        self.db = db
        self.hub = hub
        self._clock = clock
        self._now = now
        self._seen: dict[str, tuple[float, asyncio.Future[dict[str, object]]]] = {}

    # -- checks shared by launch and menu ----------------------------------------------------

    def _label(self, device: str) -> str:
        info = self.registry.info(device)
        if info is None:
            raise ApiError(404, "unknown_device", "That device isn't configured.")
        return info.label

    def _check_online(self, device: str, label: str) -> None:
        status = self.registry.status(device)
        if not status.online:
            raise ApiError(503, "offline", status.message or f"{label} is offline; check it is on and on the network.")

    def _take(self, device: str, what: str, label: str) -> None:
        wait = self.registry.actions.take(device)
        if wait > 0:
            raise ApiError(429, "rate_limited", refusal(what, wait, label), retry_after=math.ceil(wait))

    def _busy(self, device: str, label: str) -> ApiError:
        self.registry.actions.refund(device)
        return ApiError(409, "busy_launching", f"{label} is already starting or stopping something; wait a moment.")

    # -- launch ------------------------------------------------------------------------------

    async def launch(self, req: LaunchRequest) -> dict[str, object]:
        device = req.device
        label = self._label(device)  # 2
        row = self.db.game(req.game) if is_game_id(req.game) else None  # 3
        if row is None:
            raise ApiError(404, "unknown_game", "That game isn't in the catalog.")
        title = row.title
        media = self.db.device_media(device, req.game)  # 4
        if media is None or not media.present:
            raise ApiError(409, "not_on_device", f"{title} isn't on {label}.")
        presence = self.db.presence([req.game]).get(req.game, {}).get(device)  # 5
        if presence is None or not presence.system_available:
            raise ApiError(409, "core_missing", f"{label} doesn't have the core for {title} ({row.system_name}).")
        self._check_online(device, label)  # 6
        self._take(device, f"launch {title}", label)  # 7

        self._purge()  # 8
        hit = self._seen.get(req.request_id)
        if hit is not None:
            self.registry.actions.refund(device)
            return await asyncio.shield(hit[1])

        if self.registry.lock(device).locked():  # 9
            raise self._busy(device, label)
        status = self.registry.status(device)  # 10
        if status.now_playing is not None and not req.replace:
            self.registry.actions.refund(device)
            resolved = self.catalog.resolve_now_playing(device)
            np_title = resolved.title if resolved else status.now_playing.media_name
            raise ApiError(
                409,
                "playing",
                f"{np_title} is running on {label}. Replace it?",
                extra={
                    "now_playing": {
                        "title": np_title,
                        "system_id": resolved.system_id if resolved else status.now_playing.system_id,
                        "game": resolved.game_id if resolved else None,
                    }
                },
            )

        # No await since check 8, so no duplicate can have slipped in before this registration.
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._seen[req.request_id] = (self._clock() + IDEMPOTENCY_TTL, future)
        try:
            result = await self._execute(req, label, title, row.system_id, media)
        except ApiError as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved: nobody may be waiting
            raise
        except BaseException:
            # Cancelled mid-flight: don't leave a duplicate waiting on a result that will never come.
            self._seen.pop(req.request_id, None)
            future.set_exception(ApiError(504, "timeout", f"The launch on {label} was interrupted; try again."))
            future.exception()
            raise
        future.set_result(result)
        return result

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, (expires, _) in self._seen.items() if expires <= now]:
            del self._seen[key]

    @staticmethod
    def _ref(system_id: str, media: DeviceMedia) -> TargetRef | None:
        """The ref the target issued. rel_path isn't stored, but it is the path suffix whose id matches."""
        parts = normalize_rel_path(media.abs_path).split("/")
        for i in range(len(parts)):
            rel = "/".join(parts[i:])
            if game_id(system_id, rel) == media.game_id:
                return TargetRef(media.media_ref, rel)
        return None

    async def _execute(
        self, req: LaunchRequest, label: str, title: str, system_id: str, media: DeviceMedia
    ) -> dict[str, object]:
        device = req.device
        requested_at = self._now().isoformat(timespec="microseconds")
        outcome, detail = "ok", None
        try:
            async with self.registry.lock(device):
                await self._call(req, label, title, system_id, media)
        except ApiError as exc:
            outcome = exc.code
            reason = exc.extra.get("reason")
            detail = reason if isinstance(reason, str) else None
            raise
        finally:
            try:
                self.db.record_launch(device, req.game, requested_at, outcome, detail)
            except Exception:
                log.exception("Could not record the launch on %s.", device)
        self.hub.publish({"type": "device", "device": device})
        return {"device": device, "game": req.game, "started": True}

    async def _call(self, req: LaunchRequest, label: str, title: str, system_id: str, media: DeviceMedia) -> None:
        device = req.device
        target = self.registry.target(device)
        assert target is not None  # an online device has a target
        gone = ApiError(410, "gone", f"The file for {title} is gone from {label}; sync again.")
        for attempt in (1, 2):
            ref = self._ref(system_id, media)
            try:
                if ref is None:
                    raise StaleRef("no usable ref")
                await target.launch(ref, replace=req.replace)
                return
            except StaleRef:
                if attempt == 2:
                    raise gone from None
                await self.catalog.sync(device)
                fresh = self.db.device_media(device, req.game)
                if fresh is None or not fresh.present:
                    raise gone from None
                media = fresh
            except LaunchError as exc:
                raise self._map(exc) from None
            except Exception:
                log.exception("Launching on %s failed unexpectedly.", device)
                raise self._generic(label) from None

    @staticmethod
    def _generic(label: str) -> ApiError:
        return ApiError(502, "launch_failed", f"{label} couldn't do that; the server log has details.")

    @staticmethod
    def _map(exc: LaunchError) -> ApiError:
        extra: dict[str, object] = {}
        if isinstance(exc, LaunchFailed) and exc.reason:
            extra["reason"] = exc.reason
        return ApiError(exc.http_status, exc.code, str(exc), extra=extra)

    # -- return to menu ----------------------------------------------------------------------

    async def return_to_menu(self, device: str) -> dict[str, object]:
        label = self._label(device)  # 2
        self._check_online(device, label)  # 6
        self._take(device, f"return {label} to its menu", label)  # 7
        lock = self.registry.lock(device)
        if lock.locked():  # 9
            raise self._busy(device, label)
        target = self.registry.target(device)
        assert target is not None
        async with lock:
            try:
                await target.return_to_menu()
            except LaunchError as exc:
                raise self._map(exc) from None
            except Exception:
                log.exception("Returning %s to its menu failed unexpectedly.", device)
                raise self._generic(label) from None
        self.hub.publish({"type": "device", "device": device})
        return {"device": device, "stopped": True}
