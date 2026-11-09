import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from cartridge.config import DeviceConfig, load_devices, load_settings
from cartridge.core import Background, Core, build_core, close_core
from cartridge.sim.harness import Simulation
from cartridge.targets.base import CatalogEntry
from cartridge.targets.zaparoo import ZaparooTarget
from tests.conftest import family
from tests.helpers import wait_for

pytestmark = pytest.mark.anyio


def sync_runs(core: Core) -> int:
    return int(core.db._conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0])


def history_rows(core: Core) -> int:
    return len([r for r in core.db.recent_history(500) if r.device == "family-room"])


def idle(core: Core) -> bool:
    return not any(core.catalog.sync_running(n) for n in core.registry.names())


async def build(sim: Simulation, tmp_path: Path, **kw: Any) -> Core:
    settings = load_settings({"REMOTE_ENABLED": "0", "DATA_DIR": str(tmp_path / "data"), "ART_ONLINE": "0"})
    devices = load_devices(sim.write_devices(tmp_path / "devices.yaml"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    return await build_core(settings, devices, http=http, **kw)


@pytest.fixture
async def running(sim: Simulation, tmp_path: Path) -> AsyncIterator[Core]:
    core = await build(sim, tmp_path)
    Background(core, interval=3600).start()
    yield core
    await close_core(core)


async def settled(core: Core, runs: int) -> None:
    await wait_for(lambda: sync_runs(core) >= runs and idle(core))
    await wait_for(lambda: history_rows(core) > 0)


async def test_build_core_creates_private_data_dir(sim: Simulation, tmp_path: Path) -> None:
    core = await build(sim, tmp_path)
    try:
        assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "data" / "cartridge.db").exists()
        assert core.art.art_dir == tmp_path / "data" / "art"
    finally:
        await close_core(core)


async def test_startup_sync_once_per_device_then_history(running: Core) -> None:
    await settled(running, 2)
    await asyncio.sleep(0.2)
    assert sync_runs(running) == 2
    state = running.catalog.sync_state("office")
    assert state is not None and state.status == "ok"


async def test_indexing_finished_triggers_sync(sim: Simulation, running: Core) -> None:
    await settled(running, 2)
    await family(sim).fake.finish_indexing()
    await wait_for(lambda: sync_runs(running) == 3)


async def test_media_stopped_ingests_history(sim: Simulation, running: Core) -> None:
    await settled(running, 2)
    before = history_rows(running)
    fake = family(sim).fake
    await fake.play(fake.media[0]["mediaId"])
    await wait_for(lambda: running.registry.status("family-room").now_playing is not None)
    await fake.quit_game()
    await wait_for(lambda: history_rows(running) == before + 1)
    assert sync_runs(running) == 2


async def test_coming_back_online_ingests_history_without_a_sync(sim: Simulation, running: Core) -> None:
    await settled(running, 2)
    fake = family(sim).fake
    before = history_rows(running)
    fake.history.insert(  # an NFC card tapped while Cartridge couldn't see the device
        0,
        {
            "systemId": "NES",
            "mediaName": "Card Game",
            "mediaPath": "",
            "startedAt": "2026-11-06T09:00:00+00:00",
            "playTime": 5,
        },
    )
    fake.refuse = True
    await wait_for(lambda: not running.registry.status("family-room").online)
    fake.refuse = False
    await fake.start()
    await wait_for(lambda: history_rows(running) == before + 1)
    assert sync_runs(running) == 2


async def test_device_events_are_published_once(sim: Simulation, running: Core) -> None:
    await settled(running, 2)
    target_events: list[str] = []
    running.registry.subscribe(lambda e: target_events.append(e.device))
    seen: list[dict[str, object]] = []

    async def collect() -> None:
        async with running.hub.subscribe("lan") as events:
            async for event in events:
                seen.append(event)

    task = asyncio.create_task(collect())
    await wait_for(lambda: running.hub.client_count("lan") == 1)
    fake = family(sim).fake
    await fake.play(fake.media[0]["mediaId"])
    await wait_for(lambda: "family-room" in target_events)
    await asyncio.sleep(0.2)
    # One hub event per target event: a second subscription would double every count.
    published = [e for e in seen if e == {"type": "device", "device": "family-room"}]
    assert len(published) == target_events.count("family-room") >= 1
    running.hub.close()
    await task


async def test_failed_first_sync_is_retried_on_reconnect(sim: Simulation, tmp_path: Path) -> None:
    fake = family(sim).fake
    fake.close_after = "media.search"
    core = await build(sim, tmp_path)
    try:
        Background(core, interval=3600).start()

        def family_failed() -> bool:
            state = core.catalog.sync_state("family-room")
            return state is not None and state.status == "failed"

        await wait_for(family_failed)
        fake.close_after = None
        await wait_for(lambda: (s := core.catalog.sync_state("family-room")) is not None and s.status == "ok")
    finally:
        await close_core(core)


async def test_periodic_sync_covers_online_devices(sim: Simulation, tmp_path: Path) -> None:
    core = await build(sim, tmp_path)
    try:
        Background(core, interval=0.05).start()
        await wait_for(lambda: sync_runs(core) >= 6)
    finally:
        await close_core(core)


async def test_interval_defaults_to_settings_hours(sim: Simulation, tmp_path: Path) -> None:
    core = await build(sim, tmp_path)
    try:
        assert Background(core).interval == core.settings.sync_interval_hours * 3600
    finally:
        await close_core(core)


async def test_art_prefetch_follows_each_sync(sim: Simulation, tmp_path: Path) -> None:
    core = await build(sim, tmp_path)
    fetched: list[list[str]] = []

    async def spy(ids: Any) -> None:
        fetched.append(list(ids))

    core.art.prefetch = spy  # type: ignore[method-assign]
    Background(core, interval=3600).start()
    try:
        await wait_for(lambda: len(fetched) >= 2)
        assert all(fetched)
    finally:
        await close_core(core)


async def test_close_core_leaves_no_tasks(sim: Simulation, tmp_path: Path) -> None:
    before = asyncio.all_tasks()
    core = await build(sim, tmp_path)
    Background(core, interval=3600).start()
    await settled(core, 2)
    await close_core(core)
    await asyncio.sleep(0)
    assert {t for t in asyncio.all_tasks() if not t.done()} <= before


class SlowTarget(ZaparooTarget):
    async def catalog(self) -> Any:
        await asyncio.sleep(3600)
        yield CatalogEntry  # pragma: no cover


async def test_close_core_with_sync_in_flight(sim: Simulation, tmp_path: Path) -> None:
    def factory(cfg: DeviceConfig) -> SlowTarget:
        return SlowTarget(cfg)

    before = asyncio.all_tasks()
    core = await build(sim, tmp_path, factory=factory)
    Background(core, interval=3600).start()
    await wait_for(lambda: core.catalog.sync_running("family-room"))
    await close_core(core)  # must not raise
    await asyncio.sleep(0)
    assert {t for t in asyncio.all_tasks() if not t.done()} <= before


async def test_history_ingest_publishes_a_device_event(sim: Simulation, running: Core) -> None:
    await settled(running, 2)
    assert running.background is not None
    async with running.hub.subscribe("lan") as events:
        await running.background._ingest("family-room")
        event = await asyncio.wait_for(anext(events), 2)
    assert event == {"type": "device", "device": "family-room"}
