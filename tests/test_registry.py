import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal

import pytest

from cartridge.config import DeviceConfig, DeviceProblem, Devices, load_devices
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import Simulation
from cartridge.targets.base import (
    CatalogEntry,
    DeviceStatus,
    HistoryEntry,
    Image,
    SystemAvailability,
    TargetEvent,
    TargetRef,
)

pytestmark = pytest.mark.anyio


class StubTarget:
    platform: Literal["mister", "batocera"] = "mister"

    def __init__(self, config: DeviceConfig, fail_start: bool = False) -> None:
        self.name = config.name
        self.model = config.model
        self.fail_start = fail_start
        self.started = False
        self.closed = False
        self.fail_close = False
        self.listeners: list[Callable[[TargetEvent], None]] = []

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("boom")
        self.started = True

    async def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close boom")

    def status(self) -> DeviceStatus:
        return DeviceStatus("online", None, "1", None)

    def subscribe(self, listener: Callable[[TargetEvent], None]) -> None:
        self.listeners.append(listener)

    def emit(self, event: TargetEvent) -> None:
        for fn in list(self.listeners):
            fn(event)

    async def systems(self) -> list[SystemAvailability]:
        return []

    def catalog(self) -> AsyncIterator[CatalogEntry]:
        raise NotImplementedError

    async def artwork(self, ref: TargetRef, max_size: int) -> Image | None:
        return None

    async def history(self, limit: int) -> list[HistoryEntry]:
        return []

    async def launch(self, ref: TargetRef, *, replace: bool = False) -> None:
        return None

    async def return_to_menu(self) -> None:
        return None


def cfg(name: str) -> DeviceConfig:
    return DeviceConfig(name, name.title(), "MiSTer", "mister", "zaparoo", "192.0.2.1")


def build(
    names: tuple[str, ...] = ("family-room", "office"),
    problems: tuple[DeviceProblem, ...] = (),
    clock: Callable[[], float] = lambda: 0.0,
    fail: str | None = None,
) -> tuple[DeviceRegistry, dict[str, StubTarget]]:
    made: dict[str, StubTarget] = {}

    def factory(c: DeviceConfig) -> StubTarget:
        made[c.name] = StubTarget(c, fail_start=c.name == fail)
        return made[c.name]

    devices = Devices(tuple(cfg(n) for n in names), problems)
    return DeviceRegistry(devices, factory, clock=clock), made


def test_unknown_name_is_none() -> None:
    reg, _ = build()
    assert reg.target("nope") is None
    assert reg.info("nope") is None


@pytest.mark.parametrize("name", ["Office", " office", "office ", "OFFICE"])
def test_lookup_is_exact(name: str) -> None:
    reg, _ = build()
    assert reg.target(name) is None
    assert reg.info(name) is None
    assert reg.target("office") is not None


def test_names_and_info() -> None:
    reg, _ = build()
    assert reg.names() == ["family-room", "office"]
    info = reg.info("office")
    assert info is not None
    assert (info.name, info.label, info.model, info.platform, info.problem) == (
        "office",
        "Office",
        "MiSTer",
        "mister",
        None,
    )


def test_misconfigured_device_has_no_target() -> None:
    problem = DeviceProblem(1, "den", "Den", "Den: host is missing.")
    reg, made = build(names=("office",), problems=(problem,))
    # config order: the problem sat at index 1, after the good device at 0
    assert reg.names() == ["office", "den"]
    assert "den" not in made
    assert reg.target("den") is None
    assert reg.status("den") == DeviceStatus("misconfigured", "misconfigured: Den: host is missing.", None, None)
    info = reg.info("den")
    assert info is not None
    assert info.problem == "Den: host is missing."
    assert info.label == "Den"
    assert reg.problems == (problem,)
    with pytest.raises(KeyError):
        reg.lock("den")


def test_problem_without_name_is_not_listed() -> None:
    reg, _ = build(problems=(DeviceProblem(0, None, "Device 1", "Device 1: needs a name."),))
    assert reg.names() == ["family-room", "office"]


def test_names_follow_config_order_with_problem_first() -> None:
    reg, _ = build(names=("office",), problems=(DeviceProblem(0, "den", "Den", "bad"),))
    assert reg.names() == ["den", "office"]


def test_status_unknown_raises_keyerror() -> None:
    reg, _ = build()
    with pytest.raises(KeyError):
        reg.status("nope")


def test_actions_allow_four_then_refuse() -> None:
    now = [100.0]
    reg, _ = build(clock=lambda: now[0])
    assert [reg.actions.take("office") for _ in range(4)] == [0.0] * 4
    wait = reg.actions.take("office")
    assert wait == pytest.approx(15.0)
    assert reg.actions.take("family-room") == 0.0
    now[0] += 15.0
    assert reg.actions.take("office") == 0.0


def test_locks_are_per_device() -> None:
    reg, _ = build()
    assert reg.lock("office") is reg.lock("office")
    assert reg.lock("office") is not reg.lock("family-room")
    with pytest.raises(KeyError):
        reg.lock("nope")


async def test_subscribe_before_and_after_start() -> None:
    reg, made = build()
    early: list[TargetEvent] = []
    late: list[TargetEvent] = []
    reg.subscribe(early.append)
    await reg.start()
    reg.subscribe(late.append)
    made["office"].emit(TargetEvent("office", "status"))
    made["family-room"].emit(TargetEvent("family-room", "media.started"))
    assert [e.device for e in early] == ["office", "family-room"]
    assert late == early


async def test_raising_listener_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    reg, made = build()
    good: list[TargetEvent] = []

    def bad(_: TargetEvent) -> None:
        raise RuntimeError("listener broke")

    reg.subscribe(bad)
    reg.subscribe(good.append)
    with caplog.at_level(logging.ERROR):
        made["office"].emit(TargetEvent("office", "status"))
    assert len(good) == 1
    assert "listener broke" in caplog.text


async def test_start_starts_all_and_survives_a_raising_target(caplog: pytest.LogCaptureFixture) -> None:
    reg, made = build(names=("a", "b", "c"), fail="b")
    with caplog.at_level(logging.WARNING):
        await reg.start()
    assert made["a"].started and made["c"].started
    assert not made["b"].started
    assert "b" in caplog.text


async def test_start_is_concurrent() -> None:
    gate = asyncio.Event()
    seen: list[str] = []

    class Waiting(StubTarget):
        async def start(self) -> None:
            seen.append(self.name)
            await asyncio.wait_for(gate.wait(), 1)

    devices = Devices((cfg("a"), cfg("b")), ())
    reg = DeviceRegistry(devices, Waiting)
    task = asyncio.create_task(reg.start())
    for _ in range(10):
        await asyncio.sleep(0)
        if len(seen) == 2:
            break
    assert sorted(seen) == ["a", "b"]
    gate.set()
    await task


async def test_close_closes_all_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    reg, made = build()
    made["family-room"].fail_close = True
    with caplog.at_level(logging.WARNING):
        await reg.close()
    assert made["family-room"].closed and made["office"].closed


async def test_registry_over_sim(sim: Simulation, tmp_path: Path) -> None:
    path = sim.write_devices(tmp_path / "devices.yaml")
    reg = DeviceRegistry(load_devices(path))
    events: list[TargetEvent] = []
    reg.subscribe(events.append)
    await reg.start()
    try:
        assert reg.names() == ["family-room", "office"]
        for _ in range(300):
            if all(reg.status(n).online for n in reg.names()):
                break
            await asyncio.sleep(0.01)
        assert all(reg.status(n).state == "online" for n in reg.names())
        assert events
    finally:
        await reg.close()
