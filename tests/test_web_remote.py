import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from cartridge.core import Core
from cartridge.db import Database
from cartridge.ids import game_id
from cartridge.sim.harness import Simulation
from cartridge.web.common import SECURITY_HEADERS, read_routes
from cartridge.web.lan import LAN_WRITE_ROUTES
from cartridge.web.remote import build_remote_app
from tests.helpers import jwt_header, make_verifier, scope, token

pytestmark = pytest.mark.anyio

SONIC = game_id("Genesis", "Sonic the Hedgehog (USA, Europe).md")


@pytest.fixture
async def remote(core: Core) -> AsyncIterator[httpx.AsyncClient]:
    app = build_remote_app(core, make_verifier())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://games.example.com") as client:
        yield client


async def sse_frames(app: Any, headers: list[tuple[bytes, bytes]], count: int = 3) -> list[str]:
    """The first ``count`` non-ping SSE frames, read at the ASGI level (httpx buffers streaming bodies)."""
    chunks: asyncio.Queue[bytes] = asyncio.Queue()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.put_nowait(message.get("body", b""))

    task = asyncio.create_task(app(scope("/api/events", headers), inbox.get, send))
    frames: list[str] = []
    buf = b""
    try:
        while len(frames) < count:
            while b"\n\n" not in buf:
                buf += await asyncio.wait_for(chunks.get(), 5)
            raw, _, buf = buf.partition(b"\n\n")
            if not raw.startswith(b":"):
                frames.append(raw.decode())
    finally:
        inbox.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(task, 5)
    return frames


def write_body() -> bytes:
    body = {"device": "family-room", "game": SONIC, "replace": False, "request_id": str(uuid.uuid4())}
    return json.dumps(body).encode()


@pytest.mark.parametrize(("method", "path"), LAN_WRITE_ROUTES)
async def test_every_write_route_404s(remote: httpx.AsyncClient, sim: Simulation, method: str, path: str) -> None:
    before = [len(d.fake.received) for d in sim.devices]
    headers = {**jwt_header(), "Content-Type": "application/json", "X-Cartridge": "1"}
    r = await remote.request(method, path.format(name="family-room", id=SONIC), content=write_body(), headers=headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert [len(d.fake.received) for d in sim.devices] == before


async def test_no_jwt_forbidden(remote: httpx.AsyncClient) -> None:
    for path in ("/api/games", "/api/config", "/", "/api/events", "/nope"):
        r = await remote.get(path)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", path


@pytest.mark.parametrize("bad", [{"aud": "other"}, {"exp_in": -120}], ids=["aud", "expired"])
async def test_bad_jwt_forbidden(remote: httpx.AsyncClient, bad: dict[str, Any]) -> None:
    r = await remote.get("/api/games", headers=jwt_header(**bad))
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


async def test_forbidden_carries_security_headers(remote: httpx.AsyncClient) -> None:
    r = await remote.get("/api/games")
    assert r.status_code == 403
    assert r.headers["content-security-policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_valid_jwt_reads(remote: httpx.AsyncClient) -> None:
    r = await remote.get("/api/games", headers=jwt_header())
    assert r.status_code == 200 and r.json()["total"] > 0
    assert r.headers["content-security-policy"] == SECURITY_HEADERS["Content-Security-Policy"]


async def test_config_is_remote_mode(remote: httpx.AsyncClient) -> None:
    r = await remote.get("/api/config", headers=jwt_header())
    assert r.json()["mode"] == "remote"


async def test_healthz_needs_no_jwt(remote: httpx.AsyncClient) -> None:
    r = await remote.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_healthz_not_ready(core: Core) -> None:
    real = core.db
    real.close()
    core.db = Database.memory()
    core.db.close()  # a closed handle: ping() reports not ready
    app = build_remote_app(core, make_verifier())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://x") as client:
        r = await client.get("/healthz")
    assert r.status_code == 503 and r.json() == {"status": "not_ready"}
    core.db = Database.memory()  # the fixture's teardown closes it again


def test_remote_routes_are_read_only() -> None:
    """Structural: nothing in the remote route list can write, whatever a future edit adds."""
    routes = read_routes(None, "remote")  # type: ignore[arg-type]  # building routes touches no core
    mounts = [r for r in routes if isinstance(r, Mount)]
    assert len(mounts) == 1 and mounts[0].path == "/static" and isinstance(mounts[0].app, StaticFiles)
    for route in routes:
        if isinstance(route, Mount):
            continue
        assert isinstance(route, Route), f"unexpected route type {type(route).__name__}"
        assert route.methods is not None and route.methods <= {"GET", "HEAD"}, route.path
    assert not [r for r in routes if getattr(r, "path", "") == "/healthz"]


async def test_forwarded_headers_and_any_host_are_fine(remote: httpx.AsyncClient) -> None:
    headers = {**jwt_header(), "cf-connecting-ip": "198.51.100.7", "X-Forwarded-For": "198.51.100.7"}
    r = await remote.get("/api/games", headers={**headers, "Host": "evil.example.net"})
    assert r.status_code == 200


async def test_sse_uses_remote_pool(core: Core, monkeypatch: pytest.MonkeyPatch) -> None:
    modes: list[str] = []
    subscribe = core.hub.subscribe

    def spy(mode: Any) -> Any:
        modes.append(mode)
        return subscribe(mode)

    monkeypatch.setattr(core.hub, "subscribe", spy)
    app = build_remote_app(core, make_verifier())
    headers = [(b"host", b"games.example.com"), (b"cf-access-jwt-assertion", token().encode())]
    frames = await sse_frames(app, headers, 1)
    assert frames[0].startswith("event: hello") and modes == ["remote"]
