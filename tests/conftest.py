import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from cartridge.config import load_devices, load_settings
from cartridge.core import Background, Core, build_core, close_core
from cartridge.db import Database
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import SimDevice, Simulation
from cartridge.web.lan import build_lan_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def sim(tmp_path: Path) -> AsyncIterator[Simulation]:
    s = Simulation(tmp_path)
    await s.add("family_room")
    await s.add("office")
    yield s
    await s.stop()


def family(sim: Simulation) -> SimDevice:
    return sim.devices[0]


def office(sim: Simulation) -> SimDevice:
    return sim.devices[1]


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database.memory()
    yield database
    database.close()


@pytest.fixture
async def registry(sim: Simulation, tmp_path: Path) -> AsyncIterator[DeviceRegistry]:
    reg = DeviceRegistry(load_devices(sim.write_devices(tmp_path / "devices.yaml")))
    await reg.start()
    for _ in range(500):
        if all(reg.status(n).online for n in reg.names()):
            break
        await asyncio.sleep(0.01)
    yield reg
    await reg.close()


@pytest.fixture
async def core(sim: Simulation, tmp_path: Path) -> AsyncIterator[Core]:
    """A synced core over the simulated devices, built the way `serve` builds it; art never leaves the machine."""
    settings = load_settings({"REMOTE_ENABLED": "0", "DATA_DIR": str(tmp_path / "data"), "ART_ONLINE": "0"})
    devices = load_devices(sim.write_devices(tmp_path / "devices.yaml"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    built = await build_core(settings, devices, http=http)
    # An interval this long never fires; the startup sync and history ingest are what the tests start from.
    Background(built, interval=86400).start()
    for _ in range(1000):
        states = [built.catalog.sync_state(n) for n in built.registry.names()]
        synced = all(st is not None and st.status and not st.running for st in states)
        if synced and built.db.recent_history(1):
            break
        await asyncio.sleep(0.01)
    yield built
    await close_core(built)


@pytest.fixture
async def lan(core: Core) -> AsyncIterator[httpx.AsyncClient]:
    app = build_lan_app(core, frozenset({"cartridge.local"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://cartridge.local") as client:
        yield client
