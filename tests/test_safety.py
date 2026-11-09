"""The allow-list is the safety boundary (spec §9.3): these tests pin it."""

import pytest

from cartridge.sim.harness import Simulation
from cartridge.zaparoo_client import ALLOWED_METHODS, ZaparooClient

from .conftest import family

pytestmark = pytest.mark.anyio


def test_allow_list_is_exactly_the_spec() -> None:
    spec = frozenset(
        {
            "version",
            "media",
            "media.search",
            "media.meta",
            "media.image",
            "media.active",
            "media.history",
            "systems",
            "launchers",
            "run",
            "stop",
        }
    )
    assert spec == ALLOWED_METHODS


@pytest.mark.parametrize(
    "method",
    [
        "settings.update",
        "device.power.reboot",
        "update.apply",
        "input.keyboard",
        "media.generate",
        "readers.write",
        "clients.pair",
    ],
)
def test_dangerous_methods_absent(method: str) -> None:
    assert method not in ALLOWED_METHODS


async def connected(sim: Simulation) -> ZaparooClient:
    client = ZaparooClient("Family Room", "127.0.0.1", family(sim).fake.port, setup_hint="Install Zaparoo.")
    await client.start()
    await client.tried.wait()
    assert client.connected
    return client


async def test_disallowed_method_raises_before_socket(sim: Simulation) -> None:
    client = await connected(sim)
    try:
        before = list(family(sim).fake.received)
        with pytest.raises(ValueError, match="media.generate"):
            await client.call("media.generate")
        assert family(sim).fake.received == before
    finally:
        await client.stop()


async def test_unsafe_param_refused(sim: Simulation) -> None:
    client = await connected(sim)
    try:
        before = list(family(sim).fake.received)
        with pytest.raises(ValueError, match="unsafe"):
            await client.call("run", {"text": "x", "unsafe": True})
        assert family(sim).fake.received == before
    finally:
        await client.stop()
