import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
import pytest

from cartridge import zaparoo_client as zc
from cartridge.sim.harness import Simulation
from cartridge.zaparoo_client import (
    EncryptionRequired,
    ZaparooClient,
    ZaparooError,
    ZaparooTimeout,
    ZaparooUnavailable,
    describe_failure,
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


async def test_encryption_retries_back_off(
    sim: Simulation, client: ZaparooClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(zc, "BACKOFF_MAX", 0.4)
    fake = family(sim).fake
    fake.require_encryption = True
    await client.start()
    await asyncio.sleep(0.8)
    # delays 0.05, 0.1, 0.2, 0.4, ... allow about 5 attempts; a flat 0.05 would make about 16
    assert 2 <= fake.methods.count("version") <= 7


async def test_silent_link_is_dropped_and_reconnected(
    sim: Simulation, client: ZaparooClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(zc, "PING_EVERY", 0.05)
    monkeypatch.setattr(zc, "SILENCE_LIMIT", 0.15)
    seen: list[tuple[bool, str | None]] = []
    client.add_state_listener(lambda up: seen.append((up, client.error)))
    await client.start()
    await until(lambda: len(seen) == 1)
    ws = client._ws
    assert ws is not None
    real_send = ws.send_str

    async def lose_pings(data: str, *args: Any, **kwargs: Any) -> None:
        if data != "ping":  # a powered-off MiSTer answers nothing
            await real_send(data, *args, **kwargs)

    monkeypatch.setattr(ws, "send_str", lose_pings)
    await until(lambda: len(seen) == 3)
    assert seen[1][0] is False and seen[1][1] is not None and "stopped answering" in seen[1][1]
    assert seen[2][0] is True and client.connected


async def test_pongs_keep_an_idle_link_alive(client: ZaparooClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zc, "PING_EVERY", 0.05)
    monkeypatch.setattr(zc, "SILENCE_LIMIT", 0.15)
    states: list[bool] = []
    client.add_state_listener(states.append)
    await client.start()
    await until(lambda: states == [True])
    await asyncio.sleep(0.5)
    assert states == [True] and client.connected


async def test_prime_respects_the_allow_list(sim: Simulation) -> None:
    c = ZaparooClient(
        "Family Room", "127.0.0.1", family(sim).fake.port, allowed_methods=frozenset({"systems"}), setup_hint="x"
    )
    await c.start()
    await c.tried.wait()
    try:
        assert c.connected and c.version is None
        assert "version" not in family(sim).fake.methods
    finally:
        await c.stop()


def test_failure_sentences_hide_the_host() -> None:
    host = "secret-box.example.com"
    dns = aiohttp.ClientConnectorError(
        aiohttp.client_reqrep.ConnectionKey(host, 8770, False, None, None, None, None),
        socket.gaierror(-2, f"Name or service not known {host}"),
    )
    assert (
        describe_failure(dns, "Family Room", 8770, "hint") == "No answer from Family Room: is it on and on the network?"
    )
    other = RuntimeError(f"boom talking to {host}:8770")
    text = describe_failure(other, "Family Room", 8770, "hint")
    assert host not in text and "Family Room" in text
