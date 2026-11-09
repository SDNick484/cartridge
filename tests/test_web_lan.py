import asyncio
import dataclasses
import json
import socket
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from cartridge.catalog import Catalog
from cartridge.config import load_devices
from cartridge.core import Core
from cartridge.ids import game_id
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import Simulation
from cartridge.web.common import SECURITY_HEADERS
from cartridge.web.lan import LAN_WRITE_ROUTES, build_lan_app, default_lan_hosts
from tests.conftest import family, office
from tests.helpers import W, wait_for

pytestmark = pytest.mark.anyio

SONIC = game_id("Genesis", "Sonic the Hedgehog (USA, Europe).md")
METROID = game_id("SNES", "Super Metroid (USA).sfc")
FAMILY_ONLY = game_id("NES", "Family Only Quest (USA).nes")
HOSTS = frozenset({"cartridge.local"})


def launch_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"device": "family-room", "game": SONIC, "replace": False, "request_id": str(uuid.uuid4())}
    return {**body, **over}


# -- guards ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Real-IP",
        "True-Client-IP",
        "CF-Connecting-IP",
        "CF-Ray",
        "Via",
    ],
)
async def test_forwarded_headers_refused(lan: httpx.AsyncClient, header: str) -> None:
    r = await lan.get("/api/devices", headers={header: "198.51.100.7"})
    assert r.status_code == 403 and r.json()["code"] == "bad_request"
    assert set(r.json()) == {"code", "message"}


async def test_host_not_allowed(lan: httpx.AsyncClient) -> None:
    for host in ("evil.example.com", "cartridge.local.evil.example.com"):
        r = await lan.get("/api/devices", headers={"Host": host})
        assert r.status_code == 403 and r.json()["code"] == "bad_request"


async def test_missing_host_header_refused(core: Core) -> None:
    app = build_lan_app(core, HOSTS)
    status, _, _ = await raw_request(app, "GET", "/api/devices", headers=[])
    assert status == 403


@pytest.mark.parametrize("host", ["192.0.2.5:8770", "CARTRIDGE.LOCAL", "[::1]:8770", "cartridge.local:80"])
async def test_host_variants_allowed(core: Core, host: str) -> None:
    app = build_lan_app(core, frozenset({"cartridge.local", "192.0.2.5", "::1"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://x") as client:
        r = await client.get("/api/devices", headers={"Host": host})
    assert r.status_code == 200


@pytest.mark.parametrize("host", ["::1", "[::1", "cartridge.local:abc", "cartridge.local:80:80", ""])
async def test_host_malformed_refused(core: Core, host: str) -> None:
    app = build_lan_app(core, frozenset({"cartridge.local", "::1"}))
    status, _, _ = await raw_request(app, "GET", "/api/devices", headers=[(b"host", host.encode())])
    assert status == 403


async def test_write_guards(lan: httpx.AsyncClient) -> None:
    body = json.dumps(launch_body())
    cases: list[dict[str, str]] = [
        {"Content-Type": "application/json"},  # no X-Cartridge
        {**W, "Origin": "http://evil.example.com"},
        {**W, "Origin": "null"},
        {**W, "Origin": "http://cartridge.local@evil.example.com"},
        {**W, "Origin": "http://cartridge.local/path"},
        {**W, "Origin": "ftp://cartridge.local"},
        {"X-Cartridge": "1", "Content-Type": "application/x-www-form-urlencoded"},
        {"X-Cartridge": "1", "Content-Type": "text/plain"},
        {"X-Cartridge": "1"},
        {**W, "X-Cartridge": "0"},
    ]
    for headers in cases:
        r = await lan.post("/api/launch", content=body, headers=headers)
        assert r.status_code == 403, headers
        assert r.json()["code"] == "bad_request"


async def test_matching_origin_passes(lan: httpx.AsyncClient) -> None:
    for origin in ("http://cartridge.local", "https://CARTRIDGE.local:8770"):
        body = json.dumps(launch_body(replace=True))
        r = await lan.post("/api/launch", content=body, headers={**W, "Origin": origin})
        assert r.status_code == 200, origin


async def test_json_content_type_with_charset_passes(lan: httpx.AsyncClient) -> None:
    headers = {**W, "Content-Type": "Application/JSON; charset=utf-8"}
    r = await lan.post("/api/favorites/" + SONIC, headers=headers)
    assert r.status_code == 200


def test_default_lan_hosts() -> None:
    wild = default_lan_hosts("0.0.0.0")
    assert {"localhost", "127.0.0.1", "::1", socket.gethostname().lower()} <= wild
    assert "192.0.2.9" in default_lan_hosts("192.0.2.9")
    assert "192.0.2.9" not in wild
    assert "localhost" in default_lan_hosts("::")


# -- error shape ----------------------------------------------------------------------------------


async def test_unknown_route_and_method_use_json(lan: httpx.AsyncClient) -> None:
    r = await lan.get("/nope")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = await lan.get("/static/nope.js")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = await lan.put("/api/devices", headers=W)
    assert r.status_code == 405 and r.json()["code"] == "method_not_allowed"
    assert r.headers["content-type"].startswith("application/json")


async def test_unhandled_exception_is_clean_500(core: Core, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[Any]:
        raise RuntimeError("secret /media/fat/path")

    monkeypatch.setattr(core.catalog, "systems", boom)
    app = build_lan_app(core, HOSTS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://cartridge.local") as client:
        r = await client.get("/api/systems")
    assert r.status_code == 500
    assert r.json() == {"code": "internal", "message": "Something went wrong; see the server log."}
    assert "secret" not in r.text and r.headers["x-content-type-options"] == "nosniff"


# -- launch ---------------------------------------------------------------------------------------


async def test_launch_body_schema(lan: httpx.AsyncClient, sim: Simulation) -> None:
    def calls() -> int:
        return len(family(sim).fake.received) + len(office(sim).fake.received)

    good = launch_body()
    bad_bodies: list[bytes] = [
        json.dumps({k: v for k, v in good.items() if k != "device"}).encode(),
        json.dumps({**good, "extra": 1}).encode(),
        json.dumps({**good, "replace": "yes"}).encode(),
        json.dumps({**good, "replace": 1}).encode(),
        json.dumps({**good, "request_id": "not-a-uuid"}).encode(),
        json.dumps({**good, "request_id": 5}).encode(),
        json.dumps({**good, "device": ["family-room"]}).encode(),
        json.dumps({**good, "game": None}).encode(),
        b"not json",
        b"",
        b"[]",
        b'"str"',
        b"\xff\xfe",
    ]
    before = calls()
    for content in bad_bodies:
        r = await lan.post("/api/launch", content=content, headers=W)
        assert r.status_code == 400 and r.json()["code"] == "bad_request", content
    assert calls() == before


async def test_oversized_body_refused(lan: httpx.AsyncClient) -> None:
    r = await lan.post("/api/launch", content=b" " * 100_000 + b"{}", headers=W)
    assert r.status_code == 400 and r.json()["code"] == "bad_request"


async def test_launch_end_to_end(lan: httpx.AsyncClient, sim: Simulation) -> None:
    body = launch_body()
    r = await lan.post("/api/launch", content=json.dumps(body), headers=W)
    assert r.status_code == 200
    assert r.json() == {"device": "family-room", "game": SONIC, "started": True}
    assert "run" in family(sim).fake.methods


async def test_launch_playing_conflict_payload(lan: httpx.AsyncClient, sim: Simulation, core: Core) -> None:
    fake = family(sim).fake
    media = next(m for m in fake.media if m["name"] == "Super Metroid")
    await fake.play(media["mediaId"])
    await wait_for(lambda: core.registry.status("family-room").now_playing is not None)
    r = await lan.post("/api/launch", content=json.dumps(launch_body()), headers=W)
    assert r.status_code == 409
    j = r.json()
    assert j["code"] == "playing" and j["now_playing"]["title"] == "Super Metroid"
    assert j["now_playing"]["game"] == METROID


async def test_launch_unknown_game_and_device(lan: httpx.AsyncClient) -> None:
    r = await lan.post("/api/launch", content=json.dumps(launch_body(game="../etc")), headers=W)
    assert r.status_code == 404 and r.json()["code"] == "unknown_game"
    r = await lan.post("/api/launch", content=json.dumps(launch_body(device="nope")), headers=W)
    assert r.status_code == 404 and r.json()["code"] == "unknown_device"


async def test_rate_limited_launch_sets_retry_after(lan: httpx.AsyncClient) -> None:
    last = None
    for _ in range(6):
        last = await lan.post("/api/launch", content=json.dumps(launch_body(replace=True)), headers=W)
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429 and last.json()["code"] == "rate_limited"
    assert int(last.headers["retry-after"]) >= 1


# -- headers --------------------------------------------------------------------------------------


async def test_no_cors_headers_anywhere(lan: httpx.AsyncClient) -> None:
    responses = [
        await lan.get("/api/devices"),
        await lan.get("/nope"),
        await lan.post("/api/launch", content=json.dumps(launch_body()), headers=W),
        await lan.options("/api/launch", headers={"Origin": "http://cartridge.local"}),
        await lan.options("/api/launch", headers={**W, "Access-Control-Request-Method": "POST"}),
    ]
    for r in responses:
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]


async def test_security_headers(lan: httpx.AsyncClient) -> None:
    assert SECURITY_HEADERS["Content-Security-Policy"] == "default-src 'self'; img-src 'self' data:; connect-src 'self'"
    for path in ("/", "/api/devices", "/nope", "/healthz", "/static/index.html"):
        r = await lan.get(path)
        assert r.headers["content-security-policy"] == "default-src 'self'; img-src 'self' data:; connect-src 'self'"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


async def test_index_and_static(lan: httpx.AsyncClient) -> None:
    r = await lan.get("/")
    assert r.status_code == 200 and "<title>Cartridge</title>" in r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "<script" not in r.text and "<style" not in r.text and " style=" not in r.text
    assert (await lan.get("/static/index.html")).status_code == 200


# -- read routes ----------------------------------------------------------------------------------


async def test_config_and_devices(lan: httpx.AsyncClient) -> None:
    assert (await lan.get("/api/config")).json() == {
        "mode": "lan",
        "version": "0.1.0",
        "devices_configured": 2,
        "synced": True,
    }
    devices = (await lan.get("/api/devices")).json()
    assert [d["name"] for d in devices] == ["family-room", "office"]
    d = devices[0]
    assert set(d) == {
        "name", "label", "model", "platform", "state", "message",
        "version", "now_playing", "last_launched", "sync",
    }  # fmt: skip
    assert d["state"] == "online" and d["now_playing"] is None and d["sync"]["status"] == "ok"
    assert set(d["sync"]) == {"running", "status", "finished_at", "last_good_at", "error", "seen", "added", "missing"}


async def test_devices_include_misconfigured(core: Core, tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("devices:\n  - name: den\n    label: Den\n", encoding="utf-8")
    reg = DeviceRegistry(load_devices(path))
    broken = dataclasses.replace(core, registry=reg, catalog=Catalog(core.db, reg, core.hub))
    app = build_lan_app(broken, HOSTS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://cartridge.local") as client:
        devices = (await client.get("/api/devices")).json()
    assert len(devices) == 1
    assert devices[0]["name"] == "den" and devices[0]["state"] == "misconfigured"
    assert devices[0]["message"] == "Den: host must be an IP address or a hostname."
    assert devices[0]["now_playing"] is None and devices[0]["sync"] is None


async def test_systems_and_recent(lan: httpx.AsyncClient) -> None:
    systems = (await lan.get("/api/systems")).json()
    assert systems and set(systems[0]) == {"id", "name", "manufacturer", "game_count"}
    assert (await lan.get("/api/recent")).json() == []


async def test_games_api(lan: httpx.AsyncClient) -> None:
    page = (await lan.get("/api/games", params={"limit": 2})).json()
    assert set(page) == {"games", "total", "offset"} and len(page["games"]) == 2 and page["total"] > 2
    nxt = (await lan.get("/api/games", params={"limit": 2, "offset": 2})).json()
    assert nxt["offset"] == 2 and {g["id"] for g in nxt["games"]}.isdisjoint(g["id"] for g in page["games"])

    everything = (await lan.get("/api/games", params={"limit": 500})).json()
    by_title = {g["title"]: g for g in everything["games"]}
    assert set(by_title["Family Only Quest"]) == {"id", "title", "system_id", "system_name", "favorite", "devices"}
    avail = by_title["Family Only Quest"]["devices"]
    assert avail["family-room"] == {"available": True, "reason": None}
    assert avail["office"] == {"available": False, "reason": "not_on_device"}

    office_page = (await lan.get("/api/games", params={"device": "office", "limit": 500})).json()
    assert "Family Only Quest" not in [g["title"] for g in office_page["games"]]
    all_page = (await lan.get("/api/games", params={"device": "office", "only_device": "0", "limit": 500})).json()
    assert "Family Only Quest" in [g["title"] for g in all_page["games"]]

    r = await lan.get("/api/games", params={"device": "nope"})
    assert r.status_code == 404 and r.json()["code"] == "unknown_device"
    for params in ({"limit": 0}, {"limit": 501}, {"limit": "x"}, {"offset": -1}, {"favorites": "maybe"}):
        r = await lan.get("/api/games", params=params)
        assert r.status_code == 400 and r.json()["code"] == "bad_request", params


@pytest.mark.parametrize("q", ['"', "*", "AND", "c:", "NEAR(", "'", "-", '"a" OR', "a*b"])
async def test_search_api_survives_syntax(lan: httpx.AsyncClient, q: str) -> None:
    r = await lan.get("/api/games", params={"q": q})
    assert r.status_code == 200 and "games" in r.json()


async def test_game_detail(lan: httpx.AsyncClient) -> None:
    r = await lan.get(f"/api/games/{SONIC}")
    assert r.status_code == 200 and r.json()["id"] == SONIC
    for bad, code in (("aaaaaaaaaaaaaaaa", "unknown_game"), ("short", "unknown_game"), ("..%2f..%2fetc", "not_found")):
        r = await lan.get(f"/api/games/{bad}")
        assert r.status_code == 404 and r.json()["code"] == code, bad


async def test_stray_key_error_is_a_500_not_a_404(core: Core, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(item: Any) -> Any:
        raise KeyError("bug")

    monkeypatch.setattr("cartridge.web.common.game_dto", broken)
    app = build_lan_app(core, HOSTS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://cartridge.local") as client:
        r = await client.get(f"/api/games/{SONIC}")
    assert r.status_code == 500 and r.json()["code"] == "internal"


async def test_no_paths_in_any_response(lan: httpx.AsyncClient) -> None:
    text = ""
    for path in ("/api/devices", "/api/games?limit=500", "/api/systems", "/api/recent", "/api/config"):
        text += (await lan.get(path)).text
    for word in ("media_ref", "rel_path", "abs_path", "/media/fat", "127.0.0.1", "mediaId"):
        assert word not in text


async def test_healthz(lan: httpx.AsyncClient) -> None:
    r = await lan.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert (await lan.post("/healthz", headers=W)).status_code == 405


async def test_healthz_not_ready_when_db_closed(lan: httpx.AsyncClient, core: Core) -> None:
    core.db.close()
    r = await lan.get("/healthz")
    assert r.status_code == 503 and r.json() == {"status": "not_ready"}


# -- writes ---------------------------------------------------------------------------------------


async def test_favorites_toggle(lan: httpx.AsyncClient) -> None:
    r = await lan.post(f"/api/favorites/{SONIC}", headers=W)
    assert r.status_code == 200 and r.json() == {"game": SONIC, "favorite": True}
    assert (await lan.get(f"/api/games/{SONIC}")).json()["favorite"] is True
    favs = (await lan.get("/api/games", params={"favorites": "1"})).json()
    assert [g["id"] for g in favs["games"]] == [SONIC]
    r = await lan.post(f"/api/favorites/{SONIC}", content="{}", headers=W)
    assert r.status_code == 200
    r = await lan.delete(f"/api/favorites/{SONIC}", headers=W)
    assert r.status_code == 200 and r.json() == {"game": SONIC, "favorite": False}
    for bad in ("aaaaaaaaaaaaaaaa", "nope"):
        r = await lan.post(f"/api/favorites/{bad}", headers=W)
        assert r.status_code == 404 and r.json()["code"] == "unknown_game"


async def test_no_body_routes_reject_content(lan: httpx.AsyncClient) -> None:
    for content in ('{"a": 1}', "[]", "nope"):
        r = await lan.post(f"/api/favorites/{SONIC}", content=content, headers=W)
        assert r.status_code == 400 and r.json()["code"] == "bad_request"


async def test_sync_now_202_then_429(lan: httpx.AsyncClient, core: Core) -> None:
    r = await lan.post("/api/devices/office/sync", headers=W)
    assert r.status_code == 202 and r.json() == {"device": "office", "started": True}
    await wait_for(lambda: not core.catalog.sync_running("office"))
    await asyncio.sleep(0)
    r = await lan.post("/api/devices/office/sync", headers=W)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    r = await lan.post("/api/devices/nope/sync", headers=W)
    assert r.status_code == 404 and r.json()["code"] == "unknown_device"


async def test_menu_route(lan: httpx.AsyncClient, sim: Simulation) -> None:
    r = await lan.post("/api/devices/family-room/menu", headers=W)
    assert r.status_code == 200 and r.json() == {"device": "family-room", "stopped": True}
    assert "stop" in family(sim).fake.methods
    r = await lan.post("/api/devices/nope/menu", headers=W)
    assert r.status_code == 404 and r.json()["code"] == "unknown_device"


def test_write_route_table() -> None:
    assert ("POST", "/api/launch") in LAN_WRITE_ROUTES
    assert ("DELETE", "/api/favorites/{id}") in LAN_WRITE_ROUTES
    assert len(LAN_WRITE_ROUTES) == 5


# -- art ------------------------------------------------------------------------------------------


async def test_art_route(lan: httpx.AsyncClient) -> None:
    r = await lan.get(f"/art/{METROID}")
    assert r.status_code == 200 and r.headers["etag"].startswith('"')
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] in ("public, max-age=604800", "public, max-age=3600")
    again = await lan.get(f"/art/{METROID}", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == r.headers["etag"]
    assert (
        await lan.get(f"/art/{METROID}", headers={"If-None-Match": '"other", W/' + r.headers["etag"]})
    ).status_code == 304
    assert (await lan.get(f"/art/{METROID}", headers={"If-None-Match": '"other"'})).status_code == 200
    for bad in ("..%2f..%2fetc", "aaaaaaaaaaaaaaaa", "x"):
        miss = await lan.get(f"/art/{bad}")
        assert miss.status_code == 404


async def test_placeholder_cache_is_short(lan: httpx.AsyncClient) -> None:
    r = await lan.get(f"/art/{FAMILY_ONLY}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    assert r.headers["cache-control"] == "public, max-age=3600"


# -- SSE ------------------------------------------------------------------------------------------


class Stream:
    """Drives the ASGI app directly: httpx's transport buffers streaming bodies."""

    def __init__(self, app: Any, path: str = "/api/events") -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._buf = b""
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._started = asyncio.Event()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"cartridge.local")],
            "client": ("192.0.2.50", 5000),
            "server": ("cartridge.local", 80),
        }
        self.task = asyncio.create_task(app(scope, self._inbox.get, self._send))

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            self._started.set()
        elif message["type"] == "http.response.body":
            self._chunks.put_nowait(message.get("body", b""))

    async def started(self) -> int:
        await asyncio.wait_for(self._started.wait(), 5)
        assert self.status is not None
        return self.status

    async def frame(self, timeout: float = 5) -> str:
        while b"\n\n" not in self._buf:
            self._buf += await asyncio.wait_for(self._chunks.get(), timeout)
        raw, _, self._buf = self._buf.partition(b"\n\n")
        return raw.decode()

    async def event(self) -> tuple[str, Any]:
        while True:
            text = await self.frame()
            if text.startswith(":"):
                continue
            name, data = text.split("\n", 1)
            assert name.startswith("event: ") and data.startswith("data: ")
            return name.removeprefix("event: "), json.loads(data.removeprefix("data: "))

    def body(self) -> bytes:
        out = b""
        while not self._chunks.empty():
            out += self._chunks.get_nowait()
        return out

    async def close(self) -> None:
        self._inbox.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 5)


async def raw_request(
    app: Any, method: str, path: str, headers: list[tuple[bytes, bytes]]
) -> tuple[int, dict[str, str], bytes]:
    out: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        out.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("192.0.2.50", 5000),
        "server": ("cartridge.local", 80),
    }
    await app(scope, receive, send)
    start = next(m for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, body


async def test_sse_stream(core: Core, sim: Simulation) -> None:
    app = build_lan_app(core, HOSTS)
    s = Stream(app)
    assert await s.started() == 200
    assert s.headers["content-type"].startswith("text/event-stream") and s.headers["cache-control"] == "no-store"
    assert s.headers["content-security-policy"]
    name, data = await s.event()
    assert name == "hello" and [d["name"] for d in data] == ["family-room", "office"]
    assert core.hub.client_count("lan") == 1

    fake = family(sim).fake
    media = next(m for m in fake.media if m["name"] == "Super Metroid")
    await fake.play(media["mediaId"])
    seen: dict[str, Any] = {}
    for _ in range(10):
        name, data = await s.event()
        if name == "device" and data["now_playing"] is not None:
            seen = data
            break
    assert seen["name"] == "family-room" and seen["now_playing"]["title"] == "Super Metroid"
    assert "label" in seen and "sync" in seen
    await s.close()
    assert core.hub.client_count("lan") == 0


async def test_sse_other_events_and_ping(core: Core) -> None:
    app = build_lan_app(core, HOSTS, ping_seconds=0.05)
    s = Stream(app)
    await s.started()
    assert (await s.event())[0] == "hello"
    assert await s.frame() == ": ping"
    core.hub.publish({"type": "sync.finished", "device": "office", "status": "ok"})
    assert await s.event() == ("sync.finished", {"device": "office", "status": "ok"})
    await s.close()


async def test_sse_ends_when_hub_closes(core: Core) -> None:
    s = Stream(build_lan_app(core, HOSTS))
    await s.started()
    await s.event()
    core.hub.close()
    await asyncio.wait_for(s.task, 5)


async def test_sse_client_limit(core: Core) -> None:
    core.hub.max_clients = 2
    app = build_lan_app(core, HOSTS)
    a, b = Stream(app), Stream(app)
    for s in (a, b):
        await s.started()
        assert (await s.event())[0] == "hello"
    assert core.hub.client_count("lan") == 2

    third = Stream(app)
    assert await third.started() == 503
    await asyncio.wait_for(third.task, 5)
    assert json.loads(third.body())["code"] == "too_many_clients"

    await a.close()
    assert core.hub.client_count("lan") == 1
    fourth = Stream(app)
    assert await fourth.started() == 200
    assert (await fourth.event())[0] == "hello"
    await fourth.close()
    await b.close()
    assert core.hub.client_count("lan") == 0


async def test_sse_events_are_mapped_and_filtered(core: Core) -> None:
    s = Stream(build_lan_app(core, HOSTS))
    await s.started()
    await s.event()
    core.hub.publish({"type": "mystery", "device": "office", "secret": "/media/fat/x"})
    core.hub.publish({"type": "sync.started", "device": "office"})
    core.hub.publish({"type": "sync.finished", "device": "office", "status": "ok", "error": "/media/fat/x"})
    assert await s.event() == ("sync.started", {"device": "office", "status": None})
    assert await s.event() == ("sync.finished", {"device": "office", "status": "ok"})
    await s.close()


# -- guard hardening ------------------------------------------------------------------------------


def write_url(path: str) -> str:
    return path.format(name="family-room", id=SONIC)


@pytest.mark.parametrize(("method", "path"), LAN_WRITE_ROUTES)
async def test_every_write_route_is_guarded(lan: httpx.AsyncClient, method: str, path: str) -> None:
    url = write_url(path)
    bad_headers = [
        {"Content-Type": "application/json"},
        {**W, "Origin": "http://evil.example.com"},
        {"X-Cartridge": "1", "Content-Type": "application/x-www-form-urlencoded"},
    ]
    for headers in bad_headers:
        r = await lan.request(method, url, content=b"{}", headers=headers)
        assert r.status_code == 403 and r.json()["code"] == "bad_request", headers
    get = await lan.get(url)
    assert get.status_code == 405 and get.json()["code"] == "method_not_allowed"


@pytest.mark.parametrize(
    "extra",
    [(b"host", b"cartridge.local"), (b"origin", b"http://cartridge.local"), (b"x-cartridge", b"1")],
)
async def test_duplicate_guard_headers_refused(core: Core, extra: tuple[bytes, bytes]) -> None:
    app = build_lan_app(core, HOSTS)
    headers = [
        (b"host", b"cartridge.local"),
        (b"content-type", b"application/json"),
        (b"x-cartridge", b"1"),
        (b"origin", b"http://cartridge.local"),
        extra,
    ]
    status, _, body = await raw_request(app, "POST", f"/api/favorites/{SONIC}", headers=headers)
    assert status == 403 and json.loads(body)["code"] == "bad_request"


async def test_request_id_must_be_canonical(lan: httpx.AsyncClient, core: Core) -> None:
    raw = uuid.uuid4()
    for rid in (f"--{raw.hex}--", raw.hex, f"{{{raw}}}", f"urn:uuid:{raw}"):
        r = await lan.post("/api/launch", content=json.dumps(launch_body(request_id=rid)), headers=W)
        assert r.status_code == 400 and r.json()["code"] == "bad_request", rid
    r = await lan.post("/api/launch", content=json.dumps(launch_body(request_id=str(raw).upper())), headers=W)
    assert r.status_code == 200
    assert str(raw) in core.launcher._seen  # passed on in lowercase
