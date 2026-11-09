import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from cartridge.art import ArtCache
from cartridge.catalog import Catalog
from cartridge.config import load_devices, load_settings
from cartridge.core import Core
from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.launch import LaunchService
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
async def core(sim: Simulation, db: Database, registry: DeviceRegistry, tmp_path: Path) -> AsyncIterator[Core]:
    """A synced core over the simulated devices; art never leaves the machine."""
    settings = load_settings({"REMOTE_ENABLED": "0", "DATA_DIR": str(tmp_path / "data"), "ART_ONLINE": "0"})
    hub = EventHub()
    registry.subscribe(lambda e: hub.publish({"type": "device", "device": e.device}))
    catalog = Catalog(db, registry, hub)
    await catalog.sync("family-room")
    await catalog.sync("office")
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    art = ArtCache(db, registry, catalog, tmp_path / "art", http=http, online=False, version="0.1.0")
    yield Core(settings, db, registry, catalog, art, hub, LaunchService(catalog, registry, db, hub))
    await art.close()
    await http.aclose()


@pytest.fixture
async def lan(core: Core) -> AsyncIterator[httpx.AsyncClient]:
    app = build_lan_app(core, frozenset({"cartridge.local"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://cartridge.local") as client:
        yield client
