"""The leak test (spec 9.1): no JSON either app sends may contain a path or a host.

One scenario drives both apps over the synced simulation, after a launch, a quit and a history
ingest, so now-playing, last-launched and recent are all populated. Every string in every JSON body
(keys included) and in the first SSE frames is checked against the paths and hosts the server knows.
"""

import asyncio
import json
import unicodedata
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from cartridge.core import Core
from cartridge.sim.harness import Simulation
from cartridge.web.lan import LAN_WRITE_ROUTES, build_lan_app
from cartridge.web.remote import build_remote_app
from tests.conftest import family, office
from tests.helpers import W, make_verifier, scope, token, wait_for
from tests.test_web_lan import METROID, SONIC

pytestmark = pytest.mark.anyio

LAN_HOST = "cartridge.local"
REMOTE_HOST = "games.example.com"
TITLES = ("Super Metroid", "Sonic the Hedgehog")


def strings(value: Any) -> Iterator[str]:
    """Every string in a decoded JSON value: dict keys and values, recursively."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def leaks(bodies: list[tuple[str, Any]], needles: list[str]) -> list[tuple[str, str, str]]:
    return [(label, needle, s) for label, body in bodies for s in strings(body) for needle in needles if needle in s]


def needle_list(core: Core, sim: Simulation) -> list[str]:
    raw: set[str] = {"/media/", "127.0.0.1", LAN_HOST}
    for dev in sim.devices:
        raw.update((dev.fake.host, f"{dev.fake.host}:{dev.fake.port}"))
        for m in dev.fake.media:
            raw.update((m["path"], m["relativePath"]))
    rows = core.db._conn.execute("SELECT abs_path, rel_path FROM device_media").fetchall()
    raw.update(str(v) for row in rows for v in row)
    return sorted({unicodedata.normalize(form, text) for text in raw if text for form in ("NFC", "NFD")})


async def sse_bodies(core: Core, app: Any, headers: list[tuple[bytes, bytes]], mode: str) -> list[tuple[str, Any]]:
    """hello plus two published events, read at the ASGI level (httpx buffers streaming bodies)."""
    chunks: asyncio.Queue[bytes] = asyncio.Queue()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.put_nowait(message.get("body", b""))

    task = asyncio.create_task(app(scope("/api/events", headers), inbox.get, send))
    await wait_for(lambda: core.hub.client_count(mode) == 1)  # type: ignore[arg-type]
    core.hub.publish({"type": "device", "device": "family-room"})
    core.hub.publish({"type": "sync.started", "device": "office"})
    out: list[tuple[str, Any]] = []
    buf = b""
    try:
        while len(out) < 3:
            while b"\n\n" not in buf:
                buf += await asyncio.wait_for(chunks.get(), 5)
            raw, _, buf = buf.partition(b"\n\n")
            if raw.startswith(b":"):
                continue
            name, data = raw.decode().split("\n", 1)
            out.append((f"{mode} sse {name.removeprefix('event: ')}", json.loads(data.removeprefix("data: "))))
    finally:
        inbox.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(task, 5)
    return out


def launch_body(device: str, game: str) -> str:
    return json.dumps({"device": device, "game": game, "replace": False, "request_id": str(uuid.uuid4())})


def test_walker_finds_planted_leaks() -> None:
    body = {"ok": ["fine", {"/media/fat/x": 1}], "deep": {"k": ["see 127.0.0.1:9"]}}
    found = leaks([("planted", body)], ["/media/", "127.0.0.1"])
    assert {n for _, n, _ in found} == {"/media/", "127.0.0.1"}
    nfd = unicodedata.normalize("NFD", "Pokémon.sfc")
    assert leaks([("nfd", [nfd])], [nfd]) and not leaks([("clean", ["Pokemon"])], [nfd])


async def test_no_response_leaks_a_path_or_host(core: Core, sim: Simulation) -> None:
    verifier = make_verifier()
    lan_app = build_lan_app(core, frozenset({LAN_HOST}))
    remote_app = build_remote_app(core, verifier)
    needles = needle_list(core, sim)  # before the scenario edits the fakes' media
    bodies: list[tuple[str, Any]] = []

    def record(label: str, r: httpx.Response) -> httpx.Response:
        if r.headers.get("content-type", "").startswith("application/json"):
            bodies.append((f"{label} {r.status_code}", r.json()))
        return r

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(lan_app), base_url=f"http://{LAN_HOST}") as lan,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(remote_app),
            base_url=f"http://{REMOTE_HOST}",
            headers={"Cf-Access-Jwt-Assertion": token(), "cf-connecting-ip": "198.51.100.7"},
        ) as remote,
    ):
        # a launch, a quit and a history ingest, then a game running on the family-room device
        r = record("launch", await lan.post("/api/launch", content=launch_body("family-room", SONIC), headers=W))
        assert r.status_code == 200
        await family(sim).fake.quit_game()
        await wait_for(lambda: core.registry.status("family-room").now_playing is None)
        for name in ("family-room", "office"):
            await core.catalog.ingest_history(name)
        metroid = next(m for m in family(sim).fake.media if m["name"] == "Super Metroid")
        await family(sim).fake.play(metroid["mediaId"])
        await wait_for(lambda: core.registry.status("family-room").now_playing is not None)

        # every read route, on both apps
        for label, client in (("lan", lan), ("remote", remote)):
            for path in ("/api/config", "/api/devices", "/api/systems", "/api/recent", "/healthz"):
                if label == "remote" and path == "/healthz":
                    continue  # answered by the Access layer: {"status": ...} only, still walked below
                record(f"{label} {path}", await client.get(path))
            record(f"{label} games", await client.get("/api/games?show_unavailable=1&limit=500"))
            record(f"{label} games office", await client.get("/api/games?device=office"))
            record(f"{label} games family", await client.get("/api/games?device=family-room&only_device=0"))
            record(f"{label} games q", await client.get("/api/games?q=sonic"))
            listing = (await client.get("/api/games?show_unavailable=1&limit=500")).json()["games"]
            assert listing
            for game in listing:
                record(f"{label} game", await client.get(f"/api/games/{game['id']}"))
            record(f"{label} unknown game", await client.get("/api/games/not-a-game"))
            record(f"{label} unknown device", await client.get("/api/games?device=nope"))
            record(f"{label} 404", await client.get("/nope"))
        record("remote healthz", await remote.get("/healthz"))
        record("remote no jwt", await remote.get("/api/games", headers={"Cf-Access-Jwt-Assertion": ""}))

        # SSE: the first three frames of each app
        bodies += await sse_bodies(core, lan_app, [(b"host", LAN_HOST.encode())], "lan")
        bodies += await sse_bodies(
            core,
            remote_app,
            [(b"host", REMOTE_HOST.encode()), (b"cf-access-jwt-assertion", token().encode())],
            "remote",
        )

        # remote: every write route is a 404
        for method, path in LAN_WRITE_ROUTES:
            url = path.format(name="family-room", id=SONIC)
            record(f"remote {method} {url}", await remote.request(method, url, content=b"{}", headers=W))

        # LAN writes and error responses
        playing = record("playing", await lan.post("/api/launch", content=launch_body("family-room", SONIC), headers=W))
        assert playing.status_code == 409 and playing.json()["code"] == "playing"
        assert playing.json()["now_playing"]["game"] == METROID
        unknown = record("unknown", await lan.post("/api/launch", content=launch_body("family-room", "x"), headers=W))
        assert unknown.status_code == 404 and unknown.json()["code"] == "unknown_game"
        writes = [  # (method, path template, expected status), one per LAN_WRITE_ROUTES entry but launch and menu
            ("POST", "/api/devices/{name}/sync", 202),
            ("POST", "/api/favorites/{id}", 200),
            ("DELETE", "/api/favorites/{id}", 200),
        ]
        for method, path, status in writes:
            assert (method, path) in LAN_WRITE_ROUTES
            url = path.format(name="office", id=SONIC)
            r = record(f"lan {method} {url}", await lan.request(method, url, content=b"{}", headers=W))
            assert r.status_code == status, url
        await wait_for(lambda: not core.catalog.sync_running("office"))
        menu = record("lan menu", await lan.post("/api/devices/family-room/menu", content=b"{}", headers=W))
        assert menu.status_code == 200
        await wait_for(lambda: core.registry.status("family-room").now_playing is None)

        # gone: the item vanished from the device and the launch's own resync finds out
        fake = family(sim).fake
        fake.media[:] = [m for m in fake.media if m["name"] != "Sonic the Hedgehog"]
        fake.reindex()
        gone = record("gone", await lan.post("/api/launch", content=launch_body("family-room", SONIC), headers=W))
        assert gone.status_code == 410 and gone.json()["code"] == "gone"
        assert office(sim).fake.media  # untouched

        # a launch_repair failure whose Core reason is a path
        fake.media.append({**metroid, "mediaId": 9001, "name": "Repair Me", "relativePath": "SNES/r.sfc"})
        fake.reindex()
        await lan.post("/api/devices/family-room/sync", content=b"{}", headers=W)
        await wait_for(lambda: not core.catalog.sync_running("family-room"))
        repair = next(g for g in (await lan.get("/api/games?q=repair")).json()["games"])
        fake.run_error = "launch_repair"
        fake.run_reason = f"cannot open {metroid['path']}"
        body = launch_body("family-room", repair["id"])
        failed = record("repair", await lan.post("/api/launch", content=body, headers=W))
        assert failed.status_code == 502 and failed.json()["code"] == "launch_failed"

    assert len(needles) > 10 and "/media/" in needles
    assert len(bodies) > 40
    for label in ("lan sse hello", "remote sse hello"):
        assert any(lbl == label for lbl, _ in bodies), label
    by_label = {lbl: body for lbl, body in bodies}
    assert by_label["lan /api/recent 200"] and by_label["remote /api/recent 200"]  # history was ingested
    assert any(d["now_playing"] for d in by_label["lan /api/devices 200"])  # something was running
    assert any(d["last_launched"] for d in by_label["lan /api/devices 200"])
    seen = [s for _, body in bodies for s in strings(body)]
    for title in TITLES:  # the walk ran over real catalog data
        assert any(title in s for s in seen), title
    assert leaks(bodies, needles) == []
