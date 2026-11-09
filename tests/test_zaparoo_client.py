import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from cartridge import zaparoo_client as zc
from cartridge.sim.harness import Simulation
from cartridge.zaparoo_client import (
    EncryptionRequired,
    ZaparooClient,
    ZaparooError,
    ZaparooTimeout,
    ZaparooUnavailable,
)

from .conftest import family

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zc, "BACKOFF_START", 0.05)
    monkeypatch.setattr(zc, "BACKOFF_MAX", 0.1)


@pytest.fixture
async def client(sim: Simulation) -> AsyncIterator[ZaparooClient]:
    c = ZaparooClient("Family Room", "127.0.0.1", family(sim).fake.port, setup_hint="Install Zaparoo.")
    yield c
    await c.stop()


async def until(pred: Callable[[], bool], limit: float = 3.0) -> None:
    async with asyncio.timeout(limit):
        while not pred():
            await asyncio.sleep(0.01)


async def test_connects_and_reads_version(client: ZaparooClient) -> None:
    await client.start()
    await client.tried.wait()
    assert client.connected
    assert client.version == "2.20.0"
    assert client.error is None
    assert not client.needs_pairing


async def test_concurrent_calls_matched_by_id(client: ZaparooClient) -> None:
    await client.start()
    await client.tried.wait()
    first, second, third = await asyncio.gather(
        client.call("systems", {"all": True}), client.call("launchers"), client.call("version")
    )
    assert "systems" in first and "launchers" in second and third["version"] == "2.20.0"


async def test_notification_reaches_listener(sim: Simulation, client: ZaparooClient) -> None:
    heard: list[tuple[str, dict[str, Any]]] = []
    client.add_listener(lambda m, p: heard.append((m, p)))
    await client.start()
    await client.tried.wait()
    await family(sim).fake.notify("media.indexing", {"indexing": True})
    await until(lambda: bool(heard))
    assert heard == [("media.indexing", {"indexing": True})]


async def test_drop_then_reconnect_tells_state_listeners(sim: Simulation, client: ZaparooClient) -> None:
    states: list[bool] = []
    client.add_state_listener(states.append)
    await client.start()
    await until(lambda: states == [True])
    await family(sim).fake.drop_connections()
    await until(lambda: states == [True, False, True])
    assert client.connected


async def test_error_reply_carries_category_and_reason(sim: Simulation, client: ZaparooClient) -> None:
    family(sim).fake.run_error = "launch_repair"
    await client.start()
    await client.tried.wait()
    with pytest.raises(ZaparooError) as info:
        await client.call("run", {"text": "x"})
    assert info.value.category == "launch_repair"
    assert info.value.reason == "launcher_plugin_missing"
    assert not isinstance(info.value, EncryptionRequired)


async def test_silent_core_times_out_with_a_sentence(sim: Simulation, client: ZaparooClient) -> None:
    await client.start()
    await client.tried.wait()
    family(sim).fake.silent = True
    with pytest.raises(ZaparooTimeout, match=r"Family Room didn't answer systems within 0\.1s"):
        await client.call("systems", timeout=0.1)


async def test_close_mid_call_is_unavailable(sim: Simulation, client: ZaparooClient) -> None:
    await client.start()
    await client.tried.wait()
    family(sim).fake.close_after = "run"
    with pytest.raises(ZaparooUnavailable):
        await client.call("run", {"text": "x"})


async def test_refused_connection_explains_and_recovers(sim: Simulation, client: ZaparooClient) -> None:
    fake = family(sim).fake
    fake.refuse = True
    await client.start()
    await client.tried.wait()
    assert client.error is not None
    assert "isn't running" in client.error or "No answer" in client.error
    assert not client.connected
    with pytest.raises(ZaparooUnavailable):
        await client.call("version")
    fake.refuse = False
    await fake.start()
    await until(lambda: client.connected)
    assert client.error is None


async def test_encryption_means_needs_pairing_then_recovers(sim: Simulation, client: ZaparooClient) -> None:
    fake = family(sim).fake
    fake.require_encryption = True
    states: list[bool] = []
    client.add_state_listener(states.append)
    await client.start()
    await client.tried.wait()
    assert client.needs_pairing
    assert client.error == "needs pairing (Zaparoo encryption is on)"
    assert not client.connected
    assert states == [False]
    with pytest.raises(ZaparooUnavailable):
        await client.call("version")
    await asyncio.sleep(0.3)  # several retries: still one False, still not connected
    assert states == [False] and not client.connected
    fake.require_encryption = False
    await until(lambda: client.connected)
    assert not client.needs_pairing
    assert client.error is None
    assert states == [False, True]
