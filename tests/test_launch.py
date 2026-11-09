import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from cartridge.catalog import Catalog
from cartridge.config import load_devices
from cartridge.db import Database
from cartridge.errors import ApiError
from cartridge.events import EventHub
from cartridge.ids import game_id
from cartridge.launch import IDEMPOTENCY_TTL, LaunchRequest, LaunchService
from cartridge.registry import DeviceRegistry
from cartridge.sim.fake_zaparoo import FakeZaparoo
from cartridge.sim.harness import Simulation
from tests.conftest import family, office

pytestmark = pytest.mark.anyio

SONIC = game_id("Genesis", "Sonic the Hedgehog (USA, Europe).md")
METROID = game_id("SNES", "Super Metroid (USA).sfc")
DKC = game_id("SNES", "Donkey Kong Country (USA).sfc")
FAMILY_ONLY = game_id("NES", "Family Only Quest (USA).nes")
GUNSTAR = game_id("Genesis", "Gunstar Heroes (USA).md")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Env:
    sim: Simulation
    db: Database
    registry: DeviceRegistry
    hub: EventHub
    catalog: Catalog
    svc: LaunchService
    clock: Clock

    @property
    def fake(self) -> FakeZaparoo:
        return family(self.sim).fake

    def calls(self) -> int:
        return len(self.fake.received) + len(office(self.sim).fake.received)


@pytest.fixture
async def env(sim: Simulation, db: Database, tmp_path: Path) -> AsyncIterator[Env]:
    clock = Clock()
    registry = DeviceRegistry(load_devices(sim.write_devices(tmp_path / "devices.yaml")), clock=clock)
    await registry.start()
    await wait_for(lambda: all(registry.status(n).online for n in registry.names()))
    hub = EventHub()
    catalog = Catalog(db, registry, hub)
    await catalog.sync("family-room")
    await catalog.sync("office")
    yield Env(sim, db, registry, hub, catalog, LaunchService(catalog, registry, db, hub, clock=clock), clock)
    await registry.close()


async def wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def req(game: str = SONIC, device: str = "family-room", replace: bool = False, rid: str = "r1") -> LaunchRequest:
    return LaunchRequest(device, game, replace, rid)


def launches(db: Database) -> list[tuple[str, str, str | None]]:
    rows = db._conn.execute("SELECT device, outcome, detail FROM launches ORDER BY id").fetchall()
    return [(r["device"], r["outcome"], r["detail"]) for r in rows]


def sync_runs(db: Database) -> int:
    return int(db._conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0])


async def play(env: Env, name: str = "Super Metroid") -> None:
    media = next(m for m in env.fake.media if m["name"] == name)
    await env.fake.play(media["mediaId"])
    await wait_for(lambda: env.registry.status("family-room").now_playing is not None)


async def fill_rate_limit(env: Env) -> None:
    for i in range(4):
        await env.svc.launch(req(rid=f"fill{i}", replace=True))


async def hold_lock(env: Env) -> None:
    await env.registry.lock("family-room").acquire()


async def go_offline(env: Env) -> None:
    office(env.sim).fake.refuse = True
    await wait_for(lambda: not env.registry.status("office").online)


Setup = Callable[[Env], Awaitable[None]]


async def nothing(env: Env) -> None:
    return None


REJECTIONS: list[tuple[str, LaunchRequest, Setup, int, str]] = [
    ("unknown-device", req(device="nope"), nothing, 404, "unknown_device"),
    ("bad-game-id", req(game="../x"), nothing, 404, "unknown_game"),
    ("unknown-game", req(game="aaaaaaaaaaaaaaaa"), nothing, 404, "unknown_game"),
    ("not-on-device", req(FAMILY_ONLY, "office"), nothing, 409, "not_on_device"),
    ("core-missing", req(GUNSTAR, "office"), nothing, 409, "core_missing"),
    ("offline", req(METROID, "office"), go_offline, 503, "offline"),
    ("rate-limited", req(rid="fifth"), fill_rate_limit, 429, "rate_limited"),
    ("busy", req(), hold_lock, 409, "busy_launching"),
    ("playing", req(), play, 409, "playing"),
]


@pytest.mark.parametrize("case", REJECTIONS, ids=[c[0] for c in REJECTIONS])
async def test_rejections_reach_no_device(env: Env, case: tuple[str, LaunchRequest, Setup, int, str]) -> None:
    _, request, setup, status, code = case
    await setup(env)
    before, rows = env.calls(), launches(env.db)
    tokens = env.registry.actions.get(request.device).tokens if env.registry.info(request.device) else 0
    with pytest.raises(ApiError) as caught:
        await env.svc.launch(request)
    err = caught.value
    assert (err.status, err.code) == (status, code)
    assert env.calls() == before
    assert launches(env.db) == rows
    assert err.message and "127.0.0.1" not in err.message and "/media/" not in err.message
    if code == "rate_limited":
        assert err.retry_after is not None and err.retry_after >= 1
        assert "Family Room" in err.message
    if code == "playing":
        assert err.extra["now_playing"] == {"title": "Super Metroid", "system_id": "SNES", "game": METROID}
    if code in ("busy_launching", "playing"):
        assert env.registry.actions.get(request.device).tokens == tokens  # the token was refunded


async def test_rate_limit_retry_after_follows_the_clock(env: Env) -> None:
    await fill_rate_limit(env)
    with pytest.raises(ApiError) as caught:
        await env.svc.launch(req(rid="x"))
    assert caught.value.retry_after == math.ceil(15)
    env.clock.t += 15
    assert (await env.svc.launch(req(rid="y", replace=True)))["started"] is True


async def test_success_launches_and_logs(env: Env) -> None:
    events: list[dict[str, object]] = []
    async with env.hub.subscribe("lan") as stream:
        result = await env.svc.launch(req())
        events.append(await anext(stream))
    assert result == {"device": "family-room", "game": SONIC, "started": True}
    assert env.fake.active is not None and env.fake.active["mediaName"] == "Sonic the Hedgehog"
    assert launches(env.db) == [("family-room", "ok", None)]
    last = env.catalog.last_launched("family-room")
    assert last is not None and last.game_id == SONIC
    assert events == [{"type": "device", "device": "family-room"}]


async def test_replace_flow(env: Env) -> None:
    await play(env)
    start = len(env.fake.received)
    await env.svc.launch(req(replace=True))
    methods = env.fake.methods[start:]
    assert "stop" in methods and "run" in methods and methods.index("stop") < methods.index("run")


async def test_double_tap_is_idempotent(env: Env) -> None:
    env.fake.run_delay = 0.2
    a, b = await asyncio.gather(env.svc.launch(req()), env.svc.launch(req()))
    assert a == b
    assert env.fake.methods.count("run") == 1
    assert env.registry.actions.get("family-room").tokens == 3
    assert len(launches(env.db)) == 1


async def test_duplicate_after_completion_and_ttl(env: Env) -> None:
    first = await env.svc.launch(req())
    assert await env.svc.launch(req()) == first
    assert env.fake.methods.count("run") == 1
    env.clock.t += IDEMPOTENCY_TTL + 1
    await env.svc.launch(req(replace=True))
    assert env.fake.methods.count("run") == 2


async def test_duplicate_of_failed_launch_reraises(env: Env) -> None:
    env.fake.run_error = "blocked"
    with pytest.raises(ApiError) as first:
        await env.svc.launch(req())
    with pytest.raises(ApiError) as second:
        await env.svc.launch(req())
    assert second.value is first.value
    assert env.fake.methods.count("run") == 1


async def test_rate_limit_shared_with_menu(env: Env) -> None:
    for i in range(3):
        await env.svc.launch(req(rid=f"l{i}", replace=True))
    await env.svc.return_to_menu("family-room")
    before = env.calls()
    with pytest.raises(ApiError) as caught:
        await env.svc.launch(req(rid="l4", replace=True))
    assert (caught.value.status, caught.value.code) == (429, "rate_limited")
    assert env.calls() == before


async def test_reindex_triggers_one_resync(env: Env) -> None:
    env.fake.reindex()
    runs, start = sync_runs(env.db), len(env.fake.received)
    result = await env.svc.launch(req())
    assert result["started"] is True
    after = env.fake.methods[start:]
    assert sync_runs(env.db) == runs + 1  # the target's own refresh also asks for systems, so count runs
    assert after.index("media.search") < after.index("run")


async def test_gone_after_resync(env: Env) -> None:
    env.fake.media[:] = [m for m in env.fake.media if m["name"] != "Sonic the Hedgehog"]
    env.fake.reindex()
    runs, start = sync_runs(env.db), len(env.fake.received)
    with pytest.raises(ApiError) as caught:
        await env.svc.launch(req())
    assert (caught.value.status, caught.value.code) == (410, "gone")
    assert "sync again" in caught.value.message
    after = env.fake.methods[start:]
    assert sync_runs(env.db) == runs + 1 and "media.search" in after and "run" not in after
    assert launches(env.db) == [("family-room", "gone", None)]


@pytest.mark.parametrize(
    ("switch", "status", "code", "detail"),
    [
        ("busy", 409, "device_busy", None),
        ("blocked", 409, "blocked", None),
        ("launch_repair", 502, "launch_failed", "launcher_plugin_missing"),
        ("media_not_found", 410, "gone", None),
        ("slow", 504, "timeout", None),
    ],
)
async def test_run_errors_map(
    env: Env, monkeypatch: pytest.MonkeyPatch, switch: str, status: int, code: str, detail: str | None
) -> None:
    if switch == "slow":
        monkeypatch.setattr("cartridge.targets.zaparoo.RUN_TIMEOUT", 0.1)
        env.fake.run_delay = 1.0
    else:
        env.fake.run_error = switch
    with pytest.raises(ApiError) as caught:
        await env.svc.launch(req())
    err = caught.value
    assert (err.status, err.code) == (status, code)
    assert err.extra.get("reason") == detail
    assert launches(env.db) == [("family-room", code, detail)]
    assert "127.0.0.1" not in err.message and "/media/" not in err.message


async def test_unexpected_target_error_is_generic_502(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    target = env.registry.target("family-room")
    assert target is not None

    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret /media/fat host 10.0.0.9")

    monkeypatch.setattr(target, "launch", boom)
    with pytest.raises(ApiError) as caught:
        await env.svc.launch(req())
    assert (caught.value.status, caught.value.code) == (502, "launch_failed")
    assert "secret" not in caught.value.message and "10.0.0.9" not in caught.value.message
    assert launches(env.db) == [("family-room", "launch_failed", None)]


async def test_device_drops_during_run(env: Env) -> None:
    env.fake.close_after = "run"
    with pytest.raises(ApiError) as caught:
        await env.svc.launch(req())
    assert (caught.value.status, caught.value.code) == (503, "offline")
    assert launches(env.db) == [("family-room", "offline", None)]


async def test_menu_flow(env: Env) -> None:
    await play(env)
    start = len(env.fake.received)
    result = await env.svc.return_to_menu("family-room")
    assert result == {"device": "family-room", "stopped": True}
    assert "stop" in env.fake.methods[start:]


async def test_menu_unknown_device(env: Env) -> None:
    before = env.calls()
    with pytest.raises(ApiError) as caught:
        await env.svc.return_to_menu("nope")
    assert (caught.value.status, caught.value.code) == (404, "unknown_device")
    assert env.calls() == before


async def test_menu_rejections_reach_no_device(env: Env) -> None:
    await go_offline(env)
    before = env.calls()
    with pytest.raises(ApiError) as caught:
        await env.svc.return_to_menu("office")
    assert caught.value.code == "offline"
    await hold_lock(env)
    with pytest.raises(ApiError) as caught:
        await env.svc.return_to_menu("family-room")
    assert caught.value.code == "busy_launching"
    assert env.registry.actions.get("family-room").tokens == 4
    assert env.calls() == before
