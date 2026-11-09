"""Maps device config to launch targets by exact configured name (spec 5, 6.1).

There is no fuzzy lookup: ``"Office"`` and ``" office"`` are unknown. A device whose entry is broken
but has a usable name stays listed with its problem sentence, so the UI can show a card for it; it
never gets a target, a lock or a rate-limit key. Launches and menu presses share one ``actions``
bucket per device so a start/stop loop can't wear out a MiSTer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from cartridge.config import DeviceConfig, DeviceProblem, Devices
from cartridge.ratelimit import KeyedTokenBucket
from cartridge.targets.base import DeviceStatus, LaunchTarget, TargetEvent
from cartridge.targets.zaparoo import ZaparooTarget

log = logging.getLogger(__name__)

TargetFactory = Callable[[DeviceConfig], LaunchTarget]

ACTION_BURST = 4
ACTION_REFILL_PER_SECOND = 1 / 15


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    label: str
    model: str
    platform: str
    problem: str | None


class DeviceRegistry:
    def __init__(
        self,
        devices: Devices,
        factory: TargetFactory = ZaparooTarget,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.problems = devices.problems
        self.actions = KeyedTokenBucket(ACTION_BURST, ACTION_REFILL_PER_SECOND, clock=clock)
        self._listeners: list[Callable[[TargetEvent], None]] = []
        self._targets: dict[str, LaunchTarget] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._info: dict[str, DeviceInfo] = {}
        self._problem_by_name: dict[str, DeviceProblem] = {}

        # Rebuild file order: problems remember their index, valid devices fill the other slots.
        slots: dict[int, DeviceConfig | DeviceProblem] = {p.index: p for p in devices.problems}
        good = iter(devices.devices)
        for index in range(len(devices.devices) + len(devices.problems)):
            if index not in slots:
                slots[index] = next(good)
        for _, item in sorted(slots.items()):
            if isinstance(item, DeviceProblem):
                if item.name is not None:
                    self._info[item.name] = DeviceInfo(item.name, item.label, "", "", item.sentence)
                    self._problem_by_name[item.name] = item
                continue
            target = factory(item)
            target.subscribe(self._fan_in)
            self._targets[item.name] = target
            self._locks[item.name] = asyncio.Lock()
            self._info[item.name] = DeviceInfo(item.name, item.label, item.model, item.platform, None)

    def _fan_in(self, event: TargetEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("A device event listener failed on %s.", event.kind)

    async def start(self) -> None:
        async def one(name: str, target: LaunchTarget) -> None:
            try:
                await target.start()
            except Exception as exc:
                log.warning("%s: failed to start: %s", name, exc)

        await asyncio.gather(*(one(n, t) for n, t in self._targets.items()))

    async def close(self) -> None:
        async def one(name: str, target: LaunchTarget) -> None:
            try:
                await target.close()
            except Exception as exc:
                log.warning("%s: failed to close: %s", name, exc)

        await asyncio.gather(*(one(n, t) for n, t in self._targets.items()))

    def names(self) -> list[str]:
        return list(self._info)

    def info(self, name: str) -> DeviceInfo | None:
        return self._info.get(name)

    def target(self, name: str) -> LaunchTarget | None:
        return self._targets.get(name)

    def status(self, name: str) -> DeviceStatus:
        target = self._targets.get(name)
        if target is not None:
            return target.status()
        problem = self._problem_by_name[name]  # KeyError for a name nobody configured
        return DeviceStatus("misconfigured", f"misconfigured: {problem.sentence}", None, None)

    def subscribe(self, listener: Callable[[TargetEvent], None]) -> None:
        self._listeners.append(listener)

    def lock(self, name: str) -> asyncio.Lock:
        return self._locks[name]
