import asyncio

import pytest

from cartridge.events import EventHub, TooManyClients

pytestmark = pytest.mark.anyio


async def test_publish_reaches_two_subscribers() -> None:
    hub = EventHub()
    async with hub.subscribe("lan") as a, hub.subscribe("remote") as b:
        hub.publish({"type": "x", "n": 1})
        assert await anext(a) == {"type": "x", "n": 1}
        assert await anext(b) == {"type": "x", "n": 1}


async def test_pool_limit_is_per_pool_and_slot_is_freed() -> None:
    hub = EventHub(max_clients=2)
    async with hub.subscribe("lan"), hub.subscribe("lan"):
        assert hub.client_count("lan") == 2
        with pytest.raises(TooManyClients):
            async with hub.subscribe("lan"):
                pass
        async with hub.subscribe("remote"):
            assert hub.client_count("remote") == 1
    assert hub.client_count("lan") == 0
    assert hub.client_count("remote") == 0
    async with hub.subscribe("lan"):
        assert hub.client_count("lan") == 1


async def test_51st_lan_client_refused_by_default() -> None:
    hub = EventHub()
    subs = [hub.subscribe("lan") for _ in range(50)]
    for s in subs:
        await s.__aenter__()
    try:
        with pytest.raises(TooManyClients):
            async with hub.subscribe("lan"):
                pass
        async with hub.subscribe("remote"):
            pass
    finally:
        for s in subs:
            await s.__aexit__(None, None, None)
    assert hub.client_count("lan") == 0


async def test_close_ends_iteration() -> None:
    hub = EventHub()
    async with hub.subscribe("lan") as it:
        hub.publish({"type": "last"})
        hub.close()
        assert await anext(it) == {"type": "last"}
        with pytest.raises(StopAsyncIteration):
            await anext(it)


async def test_close_wakes_a_waiting_subscriber() -> None:
    hub = EventHub()
    async with hub.subscribe("lan") as it:
        waiter = asyncio.ensure_future(anext(it))
        await asyncio.sleep(0)
        hub.close()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiter, 1)


async def test_slow_subscriber_drops_oldest_and_never_blocks() -> None:
    hub = EventHub()
    async with hub.subscribe("lan") as it:
        for i in range(150):
            hub.publish({"type": "tick", "i": i})
        assert (await anext(it))["i"] == 50
        rest = [await anext(it) for _ in range(99)]
        assert rest[-1]["i"] == 149


async def test_publish_after_close_cannot_evict_the_end_marker() -> None:
    hub = EventHub()
    async with hub.subscribe("lan") as it:
        for i in range(100):
            hub.publish({"type": "tick", "i": i})
        hub.close()
        hub.publish({"type": "late"})
        got = [e async for e in it]
        assert len(got) == 99 and got[-1]["i"] == 99
