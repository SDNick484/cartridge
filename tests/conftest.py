from collections.abc import AsyncIterator
from pathlib import Path

import pytest

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
