import json
import unicodedata
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from cartridge.config import load_devices
from cartridge.sim.fake_zaparoo import FakeZaparoo, load_fixture
from cartridge.sim.harness import SimDevice, Simulation

from .conftest import family, office

pytestmark = pytest.mark.anyio

CLOSED = (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE)


async def rpc(
    ws: aiohttp.ClientWebSocketResponse, method: str, params: dict[str, Any] | None = None, rid: str = "1"
) -> dict[str, Any]:
    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
    reply: dict[str, Any] = json.loads(await ws.receive_str())
    return reply


def connect(dev: SimDevice, session: aiohttp.ClientSession) -> Any:
    return session.ws_connect(f"ws://127.0.0.1:{dev.fake.port}/api/v0.1")


async def test_ping_pong(sim: Simulation) -> None:
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        await ws.send_str("ping")
        assert await ws.receive_str() == "pong"


async def test_version_and_received_order(sim: Simulation) -> None:
    fake = family(sim).fake
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        assert (await rpc(ws, "version"))["result"] == {"version": "2.20.0", "platform": "mister"}
        await rpc(ws, "media")
        await rpc(ws, "systems", {"all": True})
        assert (await rpc(ws, "nope"))["error"]["code"] == -32601
    assert fake.methods == ["version", "media", "systems", "nope"]
    assert fake.received[2] == ("systems", {"all": True})


async def test_search_pages_cover_every_item_once(sim: Simulation) -> None:
    fake = family(sim).fake
    fake.page_cap = 2
    seen: list[int] = []
    cursor = None
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        for _ in range(10):
            params: dict[str, Any] = {"systems": ["SNES"], "maxResults": 100}
            if cursor:
                params["cursor"] = cursor
            result = (await rpc(ws, "media.search", params))["result"]
            assert len(result["results"]) <= 2
            seen += [r["mediaId"] for r in result["results"]]
            page = result["pagination"]
            assert page["hasNextPage"] == (page["nextCursor"] is not None)
            cursor = page["nextCursor"]
            if cursor is None:
                break
        bad = await rpc(ws, "media.search", {"cursor": "bogus"})
    assert sorted(seen) == sorted(m["mediaId"] for m in fake.media if m["system"]["id"] == "SNES")
    assert bad["error"]["code"] == -32602


async def test_search_relative_path_switch(sim: Simulation) -> None:
    fake = family(sim).fake
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        assert "relativePath" in (await rpc(ws, "media.search"))["result"]["results"][0]
        fake.search_relative_path = False
        first = (await rpc(ws, "media.search"))["result"]["results"][0]
        assert "relativePath" not in first
        meta = await rpc(ws, "media.meta", {"mediaId": first["mediaId"]})
        assert meta["result"]["media"]["relativePath"]


async def test_meta_single_and_batch(sim: Simulation) -> None:
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        one = (await rpc(ws, "media.meta", {"mediaId": 1}))["result"]["media"]
        assert one["zapScript"] == "SNES/Super Metroid (USA).sfc"
        assert one["title"]["name"] == "Super Metroid"
        assert (await rpc(ws, "media.meta", {"mediaId": 99999}))["error"]["message"] == "media not found"
        batch = (await rpc(ws, "media.meta", {"items": [{"mediaId": 1}, {"mediaId": 99999}]}))["result"]["items"]
        assert batch[0]["mediaId"] == 1 and batch[0]["media"]["path"].startswith("/media/fat/games/SNES/")
        assert batch[1] == {"mediaId": 99999, "media": None}


async def test_image(sim: Simulation) -> None:
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        ok = (await rpc(ws, "media.image", {"mediaId": 1}))["result"]
        assert ok["contentType"] == "image/png" and ok["data"]
        assert (await rpc(ws, "media.image", {"mediaId": 2}))["error"]["message"] == "no image found"


async def test_fixture_awkward_names(sim: Simulation) -> None:
    fam = next(m for m in family(sim).fake.media if m["name"].startswith("Façade"))
    off = next(m for m in office(sim).fake.media if m["name"].startswith("Façade"))
    assert fam["relativePath"] != off["relativePath"]
    assert unicodedata.normalize("NFC", fam["relativePath"]) == off["relativePath"]
    assert unicodedata.is_normalized("NFD", fam["path"])


async def test_require_encryption(sim: Simulation) -> None:
    family(sim).fake.require_encryption = True
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        err = (await rpc(ws, "version"))["error"]
    assert err == {"code": -32002, "message": "encryption required"}


async def test_run_error_category_and_repair(sim: Simulation) -> None:
    fake = family(sim).fake
    fake.run_error = "busy"
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        assert (await rpc(ws, "run", {"text": "x"}))["error"]["data"]["category"] == "busy"
        fake.run_error = "launch_repair"
        data = (await rpc(ws, "run", {"text": "x"}))["error"]["data"]
        assert data == {"category": "launch_repair", "reason": "launcher_plugin_missing"}
        fake.run_error = None
        assert (await rpc(ws, "run", {"text": "nothing"}))["error"]["data"]["category"] == "media_not_found"


async def test_run_stop_and_notifications(sim: Simulation) -> None:
    fake = family(sim).fake
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        assert (await rpc(ws, "stop"))["result"] is None  # nothing running
        await ws.send_str(json.dumps({"id": "2", "method": "run", "params": {"text": "SNES/Super Metroid (USA).sfc"}}))
        started = json.loads(await ws.receive_str())  # the notification precedes the reply
        assert json.loads(await ws.receive_str())["result"] is None
        assert started["method"] == "media.started"
        assert set(started["params"]) == {"systemId", "systemName", "mediaPath", "mediaName"}
        assert (await rpc(ws, "media.active"))["result"]["mediaName"] == "Super Metroid"
        await ws.send_str(json.dumps({"id": "3", "method": "stop"}))
        assert json.loads(await ws.receive_str())["method"] == "media.stopped"
        assert json.loads(await ws.receive_str())["result"] is None
        assert (await rpc(ws, "media.active"))["result"] is None
    assert fake.active is None and fake.history[0]["mediaName"] == "Super Metroid"


async def test_play_and_finish_indexing_notify(sim: Simulation) -> None:
    fake = family(sim).fake
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        await fake.play(2)
        assert json.loads(await ws.receive_str())["method"] == "media.started"
        await fake.quit_game()
        assert json.loads(await ws.receive_str())["method"] == "media.stopped"
        await fake.finish_indexing()
        note = json.loads(await ws.receive_str())
        assert note == {
            "jsonrpc": "2.0",
            "method": "media.indexing",
            "params": {"exists": True, "indexing": False, "totalMedia": len(fake.media)},
        }


async def test_run_with_launcher_absent(sim: Simulation) -> None:
    async with aiohttp.ClientSession() as s, connect(office(sim), s) as ws:
        err = (await rpc(ws, "run", {"text": "Genesis/Gunstar Heroes (USA).md"}))["error"]
    assert err["data"] == {"category": "launch_repair", "reason": "launcher_plugin_missing"}


async def test_history_distinct(sim: Simulation) -> None:
    fake = family(sim).fake
    await fake.play(1)
    await fake.quit_game()
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        allh = (await rpc(ws, "media.history", {"limit": 10}))["result"]["entries"]
        distinct = (await rpc(ws, "media.history", {"limit": 10, "distinctMedia": True}))["result"]["entries"]
    assert [e["mediaPath"] for e in allh].count(fake.media[0]["path"]) == 2
    assert [e["mediaPath"] for e in distinct].count(fake.media[0]["path"]) == 1
    assert distinct[0]["startedAt"] >= distinct[1]["startedAt"]
    assert "mediaId" not in allh[-1]


async def test_reindex_changes_ids(sim: Simulation) -> None:
    fake = family(sim).fake
    before = [m["mediaId"] for m in fake.media]
    fake.reindex()
    assert [m["mediaId"] for m in fake.media] == [i + 1000 for i in before]
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        assert "error" in await rpc(ws, "media.meta", {"mediaId": before[0]})
        assert "result" in await rpc(ws, "media.meta", {"mediaId": before[0] + 1000})


async def test_systems_and_launchers_office(sim: Simulation) -> None:
    async with aiohttp.ClientSession() as s, connect(office(sim), s) as ws:
        every = (await rpc(ws, "systems", {"all": True}))["result"]["systems"]
        avail = (await rpc(ws, "systems"))["result"]["systems"]
        launchers = (await rpc(ws, "launchers"))["result"]["launchers"]
    assert {x["id"] for x in every} == {"SNES", "Genesis", "NES"}
    assert {x["id"] for x in avail} == {"SNES", "NES"}
    assert next(x for x in every if x["id"] == "SNES")["mediaCount"] == 3
    assert "Genesis" not in {x["systemId"] for x in launchers}


async def test_refuse_and_restore(sim: Simulation) -> None:
    fake = family(sim).fake
    port = fake.port
    async with aiohttp.ClientSession() as s:
        ws = await connect(family(sim), s)
        fake.refuse = True
        assert (await ws.receive()).type in CLOSED
        with pytest.raises(aiohttp.ClientConnectorError):
            await connect(family(sim), s)
        fake.refuse = False
        assert await fake.start() == port
        async with connect(family(sim), s) as ws2:
            assert (await rpc(ws2, "version"))["result"]["platform"] == "mister"


async def test_close_after(sim: Simulation) -> None:
    fake = family(sim).fake
    fake.close_after = "run"
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": "1", "method": "run", "params": {"text": "x"}}))
        assert (await ws.receive()).type in CLOSED
    assert fake.methods == ["run"]


async def test_drop_connections(sim: Simulation) -> None:
    fake = family(sim).fake
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        await fake.drop_connections()
        assert (await ws.receive()).type in CLOSED


async def test_fixture_is_deep_copied() -> None:
    fixture = load_fixture("office")
    fake = FakeZaparoo(fixture)
    fake.media.clear()
    assert fixture["media"]


async def test_devices_yaml(sim: Simulation, tmp_path: Path) -> None:
    path = sim.write_devices(tmp_path / "devices.yaml")
    loaded = load_devices(path)
    assert loaded.problems == ()
    assert [d.name for d in loaded.devices] == ["family-room", "office"]
    assert loaded.devices[0].label == "Family Room"
    assert loaded.devices[1].port == office(sim).fake.port
    assert loaded.devices[1].host == "127.0.0.1"


async def test_malformed_requests_get_error_replies(sim: Simulation) -> None:
    fake = family(sim).fake
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        assert (await rpc(ws, "media.search", {"maxResults": "ten"}))["error"]["code"] == -32602
        assert (await rpc(ws, "media.history", {"limit": "x"}))["error"]["code"] == -32602
        await ws.send_str("[1, 2]")
        bad = json.loads(await ws.receive_str())
        assert bad["error"]["code"] == -32600 and bad["id"] is None
        await ws.send_str("{nope")
        assert json.loads(await ws.receive_str())["error"]["code"] == -32700
        del fake.media[0]["name"]  # a broken library: an unexpected failure in a handler
        assert (await rpc(ws, "media.search"))["error"]["code"] == -32603
        assert (await rpc(ws, "version"))["result"]["platform"] == "mister"  # still serving


async def test_history_entries_use_session_record_shape(sim: Simulation) -> None:
    fake = family(sim).fake
    await fake.play(1)
    await fake.quit_game()
    keys = {"launcherId", "zapScript", "hasCover", "startedAt", "endedAt", "playTime", "sessionSource"}
    assert all(keys | {"sessionConfidence"} <= set(e) for e in fake.history)
    assert "mediaId" not in fake.history[-1]


@pytest.mark.parametrize("after", [False, True])
async def test_started_notification_order(sim: Simulation, after: bool) -> None:
    family(sim).fake.started_after_reply = after
    async with aiohttp.ClientSession() as s, connect(family(sim), s) as ws:
        await ws.send_str(json.dumps({"id": "7", "method": "run", "params": {"text": "SNES/Super Metroid (USA).sfc"}}))
        first, second = json.loads(await ws.receive_str()), json.loads(await ws.receive_str())
    reply, note = (first, second) if after else (second, first)
    assert reply["id"] == "7" and reply["result"] is None
    assert note["method"] == "media.started"
