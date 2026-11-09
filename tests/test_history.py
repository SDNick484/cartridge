import asyncio

import pytest

from cartridge.catalog import Catalog
from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.ids import game_id
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import Simulation
from tests.conftest import family, office

pytestmark = pytest.mark.anyio

SONIC = game_id("Genesis", "Sonic the Hedgehog (USA, Europe).md")


async def synced(db: Database, registry: DeviceRegistry) -> Catalog:
    cat = Catalog(db, registry, EventHub())
    await cat.sync("family-room")
    await cat.sync("office")
    return cat


async def playing_on(registry: DeviceRegistry, device: str) -> None:
    for _ in range(500):
        if registry.status(device).now_playing is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("now playing never arrived")


async def test_ingest_maps_known_paths_and_keeps_unresolved(
    sim: Simulation, db: Database, registry: DeviceRegistry
) -> None:
    cat = await synced(db, registry)
    fake = family(sim).fake
    # An NFC card: no mediaId, an empty mediaPath.
    fake.history.insert(
        0,
        {
            "systemId": "NES",
            "mediaName": "Card Game",
            "mediaPath": "",
            "startedAt": "2026-11-06T09:00:00+00:00",
            "playTime": 5,
        },
    )
    fake.history.insert(
        0,
        {
            "systemId": "SNES",
            "mediaName": "Homebrew",
            "mediaPath": "/media/fat/games/SNES/Homebrew.sfc",
            "startedAt": "2026-11-06T10:00:00+00:00",
            "playTime": 7,
        },
    )
    await cat.ingest_history("family-room")
    rows = {r.media_name: r for r in db.recent_history(50) if r.device == "family-room"}
    assert rows["Sonic the Hedgehog"].game_id == SONIC
    assert rows["Super Metroid"].game_id is not None
    assert rows["Homebrew"].game_id is None
    assert rows["Card Game"].game_id is None and rows["Card Game"].system_id == "NES"
    assert all(r.ingested_at for r in rows.values())


async def test_recent_merges_devices_newest_first(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat = await synced(db, registry)
    await cat.ingest_history("family-room")
    await cat.ingest_history("office")
    items = cat.recent()
    assert [i.title for i in items] == ["Contra", "Super Metroid", "Sonic the Hedgehog"]
    assert [i.device for i in items] == ["office", "family-room", "family-room"]
    assert all(i.as_of is None for i in items)
    assert cat.recent(limit=1)[0].title == "Contra"


async def test_recent_keeps_offline_device_with_as_of(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat = await synced(db, registry)
    await cat.ingest_history("office")
    await cat.ingest_history("family-room")
    stamp = db.history_as_of("office")
    office(sim).fake.refuse = True
    for _ in range(500):
        if not registry.status("office").online:
            break
        await asyncio.sleep(0.01)
    assert not registry.status("office").online
    await cat.ingest_history("office")  # offline: logged, not raised
    items = {i.device: i for i in cat.recent()}
    assert items["office"].title == "Contra" and items["office"].as_of == stamp
    assert items["family-room"].as_of is None


async def test_ingest_unknown_device_raises(db: Database, registry: DeviceRegistry) -> None:
    with pytest.raises(KeyError):
        await Catalog(db, registry, EventHub()).ingest_history("nope")


async def test_now_playing_resolves_game(sim: Simulation, db: Database, registry: DeviceRegistry) -> None:
    cat = await synced(db, registry)
    assert cat.resolve_now_playing("family-room") is None
    media = next(m for m in family(sim).fake.media if m["name"] == "Super Metroid")
    await family(sim).fake.play(media["mediaId"])
    await playing_on(registry, "family-room")
    playing = cat.resolve_now_playing("family-room")
    assert playing is not None
    assert playing.game_id == game_id("SNES", "Super Metroid (USA).sfc")
    assert playing.title == "Super Metroid" and playing.system_id == "SNES"


async def test_now_playing_unindexed_path_keeps_media_name(
    sim: Simulation, db: Database, registry: DeviceRegistry
) -> None:
    cat = await synced(db, registry)
    fake = family(sim).fake
    fake.media.append(
        {**fake.media[0], "mediaId": 999, "name": "Homebrew", "path": "/media/fat/games/SNES/Homebrew.sfc"}
    )
    await fake.play(999)
    await playing_on(registry, "family-room")
    playing = cat.resolve_now_playing("family-room")
    assert playing is not None and playing.game_id is None and playing.title == "Homebrew"


async def test_now_playing_unknown_device(db: Database, registry: DeviceRegistry) -> None:
    with pytest.raises(KeyError):
        Catalog(db, registry, EventHub()).resolve_now_playing("nope")
