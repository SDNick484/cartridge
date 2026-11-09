import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from cartridge.config import load_devices
from cartridge.db import Database
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import SimDevice, Simulation


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
