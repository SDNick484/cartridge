import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from cartridge.catalog import Catalog, SyncRateLimited, utcnow
from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.ids import game_id
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import Simulation
from cartridge.targets.base import CatalogEntry, SystemAvailability, TargetRef
from tests.conftest import family

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(db: Database, registry: DeviceRegistry, clock: Clock | None = None) -> tuple[Catalog, EventHub]:
    hub = EventHub()
    if clock is None:
        return Catalog(db, registry, hub), hub
    return Catalog(db, registry, hub, clock=clock), hub


def media_rows(db: Database, device: str) -> dict[str, tuple[str, bool]]:
    rows = db._conn.execute("SELECT game_id, media_ref, present FROM device_media WHERE device = ?", (device,))
    return {r["game_id"]: (r["media_ref"], bool(r["present"])) for r in rows}


async def test_identity_across_devices(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, _ = make(db, registry)
    assert (await cat.sync("family-room")).status == "ok"
    assert (await cat.sync("office")).status == "ok"
    for title in ("Super Metroid", "Façade & Friends: Part II"):
        rows = db._conn.execute("SELECT id FROM games WHERE title = ?", (title,)).fetchall()
        assert len(rows) == 1
        gid = rows[0]["id"]
        presence = db.presence([gid])[gid]
        assert presence["family-room"].present and presence["office"].present


async def test_identity_stable_across_reindex(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, _ = make(db, registry)
    await cat.sync("family-room")
    before = media_rows(db, "family-room")
    family(sim).fake.reindex()
    result = await cat.sync("family-room")
    after = media_rows(db, "family-room")
    assert result.added == 0
    assert after.keys() == before.keys()
    assert all(after[g][0] != before[g][0] and after[g][1] for g in before)


async def test_pagination(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    family(sim).fake.page_cap = 2
    cat, _ = make(db, registry)
    result = await cat.sync("family-room")
    assert result.status == "ok"
    assert result.seen == len(family(sim).fake.media) == len(media_rows(db, "family-room"))


async def test_present_flips(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, _ = make(db, registry)
    await cat.sync("family-room")
    gone = family(sim).fake.media.pop()
    result = await cat.sync("family-room")
    assert result.missing == 1
    gid = game_id(gone["system"]["id"], gone["relativePath"])
    rows = media_rows(db, "family-room")
    assert rows[gid][1] is False
    assert sum(1 for _, p in rows.values() if p) == len(family(sim).fake.media)
    assert db.game(gid) is not None


async def test_failed_run_changes_nothing(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, hub = make(db, registry)
    await cat.sync("family-room")
    good = db.last_good_sync("family-room")
    before = media_rows(db, "family-room")
    family(sim).fake.media.pop()
    family(sim).fake.close_after = "media.search"
    async with hub.subscribe("lan") as events:
        result = await cat.sync("family-room")
        assert [(await anext(events))["type"] for _ in range(2)] == ["sync.started", "sync.finished"]
    assert result.status == "failed" and result.error
    assert "unexpectedly" not in result.error and "Family Room" in result.error
    assert media_rows(db, "family-room") == before
    last = db.last_sync("family-room")
    assert last is not None and last.status == "failed" and last.error == result.error
    assert db.last_good_sync("family-room") == good


async def test_unexpected_error_is_a_failed_run(
    sim: Simulation, db: Database, registry: DeviceRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    cat, _ = make(db, registry)

    def boom(*_: object) -> None:
        raise RuntimeError("secret detail")

    db.upsert_game = boom  # type: ignore[method-assign]
    result = await cat.sync("family-room")
    assert result.status == "failed"
    assert result.error == "Sync of Family Room failed unexpectedly; see the server log."
    assert media_rows(db, "family-room") == {}
    assert "secret detail" in caplog.text
    last = db.last_sync("family-room")
    assert last is not None and last.status == "failed"


async def test_unknown_device_is_key_error(db: Database, registry: DeviceRegistry) -> None:
    cat, _ = make(db, registry)
    with pytest.raises(KeyError):
        await cat.sync("nope")


async def test_availability_from_systems_all(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, _ = make(db, registry)
    await cat.sync("office")
    row = db._conn.execute(
        "SELECT available, media_count FROM device_systems WHERE device = 'office' AND system_id = 'Genesis'"
    ).fetchone()
    assert row["available"] == 0 and row["media_count"] > 0


async def test_concurrent_sync_joins(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, _ = make(db, registry)
    target = registry.target("family-room")
    assert target is not None
    real = target.systems
    calls: list[str] = []

    async def counting() -> list[SystemAvailability]:
        calls.append("systems")
        await asyncio.sleep(0.05)
        return await real()

    target.systems = counting  # type: ignore[method-assign]
    a, b = await asyncio.gather(cat.sync("family-room"), cat.sync("family-room"))
    assert a == b and a.status == "ok" and len(calls) == 1


async def test_cancelled_joiner_does_not_cancel_run(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, _ = make(db, registry)
    first = asyncio.ensure_future(cat.sync("family-room"))
    await asyncio.sleep(0)
    joiner = asyncio.ensure_future(cat.sync("family-room"))
    await asyncio.sleep(0)
    joiner.cancel()
    assert (await first).status == "ok"
    assert joiner.cancelled()


async def test_sync_now_rate_limited(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    clock = Clock()
    cat, _ = make(db, registry, clock)
    assert (await cat.sync_now("family-room")).status == "ok"
    clock.t += 30
    with pytest.raises(SyncRateLimited) as exc:
        await cat.sync_now("family-room")
    assert 0 < exc.value.wait <= 30
    assert (await cat.sync_now("office")).status == "ok"
    clock.t += 31
    assert (await cat.sync_now("family-room")).status == "ok"


async def test_sync_publishes_events(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat, hub = make(db, registry)
    async with hub.subscribe("lan") as events:
        await cat.sync("office")
        assert await anext(events) == {"type": "sync.started", "device": "office"}
        finished = {"type": "sync.finished", "device": "office", "status": "ok", "added": 6, "missing": 0}
        assert await anext(events) == finished


def test_utcnow_is_aware_utc() -> None:
    assert utcnow().tzinfo == UTC
    assert datetime.fromisoformat(utcnow().isoformat(timespec="microseconds")).tzinfo == UTC


async def test_unlisted_system_gets_placeholder_and_entry_is_kept(
    sim: Simulation, db: Database, registry: DeviceRegistry
) -> None:
    cat, _ = make(db, registry)
    target = registry.target("family-room")
    assert target is not None
    real = target.catalog

    async def extra() -> AsyncIterator[CatalogEntry]:
        async for e in real():
            yield e
        yield CatalogEntry("Odd", "Odd Game", "Odd Game.bin", "/media/fat/games/Odd/Odd Game.bin", TargetRef("9", "x"))

    target.catalog = extra  # type: ignore[method-assign]
    result = await cat.sync("family-room")
    assert result.status == "ok" and result.seen == len(family(sim).fake.media) + 1
    gid = game_id("Odd", "Odd Game.bin")
    assert media_rows(db, "family-room")[gid][1] is True
    assert db.game(gid) is not None


async def test_start_failure_still_returns_a_result(
    sim: Simulation, db: Database, registry: DeviceRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    cat, _ = make(db, registry)

    def boom(*_: object) -> int:
        raise RuntimeError("disk full")

    db.start_sync_run = boom  # type: ignore[method-assign]
    result = await cat.sync("family-room")
    assert result.status == "failed"
    assert result.error == "Sync of Family Room failed unexpectedly; see the server log."
    assert "disk full" in caplog.text


async def test_backwards_clock_still_flips_unseen(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    times = iter([datetime(2030, 1, 1, tzinfo=UTC)] * 2 + [datetime(2020, 1, 1, tzinfo=UTC)] * 20)
    cat = Catalog(db, registry, EventHub(), now=lambda: next(times))
    await cat.sync("family-room")
    gone = family(sim).fake.media.pop()
    result = await cat.sync("family-room")
    assert result.status == "ok" and result.missing == 1
    assert media_rows(db, "family-room")[game_id(gone["system"]["id"], gone["relativePath"])][1] is False


async def test_joining_via_sync_now_takes_no_token(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    clock = Clock()
    cat, _ = make(db, registry, clock)
    target = registry.target("family-room")
    assert target is not None
    real = target.systems
    gate = asyncio.Event()

    async def slow() -> list[SystemAvailability]:
        await gate.wait()
        return await real()

    target.systems = slow  # type: ignore[method-assign]
    first = asyncio.ensure_future(cat.sync_now("family-room"))
    await asyncio.sleep(0)
    joined = asyncio.ensure_future(cat.sync_now("family-room"))  # bucket is empty: a token take would refuse
    await asyncio.sleep(0)
    gate.set()
    assert (await first).status == (await joined).status == "ok"
    clock.t += 30  # no second token was spent: the one refill still has 30s to go
    with pytest.raises(SyncRateLimited) as exc:
        await cat.sync_now("family-room")
    assert 29 < exc.value.wait <= 30


async def test_cancelled_run_records_failed_and_reraises(
    sim: Simulation, db: Database, registry: DeviceRegistry
) -> None:
    cat, hub = make(db, registry)
    target = registry.target("family-room")
    assert target is not None
    started = asyncio.Event()

    async def hang() -> list[SystemAvailability]:
        started.set()
        await asyncio.sleep(60)
        return []

    target.systems = hang  # type: ignore[method-assign]
    async with hub.subscribe("lan") as events:
        task = asyncio.ensure_future(cat._run("family-room"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [(await anext(events))["type"] for _ in range(2)] == ["sync.started", "sync.finished"]
    last = db.last_sync("family-room")
    assert last is not None and last.status == "failed" and last.error and "interrupted" in last.error


async def test_sync_where_nothing_has_a_usable_path_fails_and_flips_nothing(
    sim: Simulation, db: Database, registry: DeviceRegistry
) -> None:
    cat, _ = make(db, registry)
    await cat.sync("family-room")
    before = media_rows(db, "family-room")
    fake = family(sim).fake
    fake.search_relative_path = False
    for m in fake.media:
        m["relativePath"] = ""
    result = await cat.sync("family-room")
    assert result.status == "failed"
    assert result.error == (
        f"Family Room reported {len(fake.media)} games but none had a usable path; "
        "check strip_prefixes in devices.yaml (see README)."
    )
    assert media_rows(db, "family-room") == before and all(p for _, p in before.values())
