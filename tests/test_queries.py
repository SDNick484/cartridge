import pytest

from cartridge.catalog import Availability, Catalog, GamePage
from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.ids import game_id
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import Simulation

pytestmark = pytest.mark.anyio

GUNSTAR = game_id("Genesis", "Gunstar Heroes (USA).md")
FAMILY_ONLY = game_id("NES", "Family Only Quest (USA).nes")


@pytest.fixture
async def cat(sim: Simulation, db: Database, registry: DeviceRegistry) -> Catalog:
    c = Catalog(db, registry, EventHub())
    await c.sync("family-room")
    await c.sync("office")
    return c


def query(
    cat: Catalog,
    *,
    system: str | None = None,
    q: str | None = None,
    device: str | None = None,
    only_device: bool = False,
    favorites: bool = False,
    show_unavailable: bool = False,
    offset: int = 0,
    limit: int = 100,
) -> GamePage:
    return cat.games(
        system=system,
        q=q,
        device=device,
        only_device=only_device,
        favorites=favorites,
        show_unavailable=show_unavailable,
        offset=offset,
        limit=limit,
    )


def titles(page: GamePage) -> list[str]:
    return [i.title for i in page.items]


async def test_only_device_excludes_other_devices_games(cat: Catalog) -> None:
    assert "Family Only Quest" in titles(query(cat))
    office_titles = titles(query(cat, device="office", only_device=True))
    assert "Family Only Quest" not in office_titles and "Contra" in office_titles


async def test_availability_per_device(cat: Catalog) -> None:
    items = {i.title: i for i in query(cat).items}
    assert items["Gunstar Heroes"].availability == {
        "family-room": Availability(True, None),
        "office": Availability(False, "core_missing"),
    }
    assert items["Family Only Quest"].availability["office"] == Availability(False, "not_on_device")
    assert items["Family Only Quest"].availability["family-room"] == Availability(True, None)
    assert items["Gunstar Heroes"].system_name == "Genesis"
    assert cat.game(GUNSTAR) == items["Gunstar Heroes"]


async def test_unknown_game_and_device(cat: Catalog) -> None:
    assert cat.game("nope") is None
    with pytest.raises(KeyError):
        query(cat, device="nope", only_device=True)


async def test_paging_and_search(cat: Catalog) -> None:
    page = query(cat, limit=2)
    assert len(page.items) == 2 and page.total == 7
    assert titles(query(cat, q="gunstar")) == ["Gunstar Heroes"]
    assert titles(query(cat, system="NES")) == ["Contra", "Family Only Quest"]


async def test_systems_only_with_present_games(cat: Catalog) -> None:
    systems = {s.id: s for s in cat.systems()}
    assert set(systems) == {"SNES", "Genesis", "NES"}
    assert systems["Genesis"].name == "Genesis" and systems["Genesis"].game_count == 2


async def test_favorites(cat: Catalog) -> None:
    assert cat.set_favorite("nope", True) is False
    assert cat.set_favorite(GUNSTAR, True) is True
    assert titles(query(cat, favorites=True)) == ["Gunstar Heroes"]
    game = cat.game(GUNSTAR)
    assert game is not None and game.favorite is True
    assert cat.set_favorite(GUNSTAR, False) is True
    assert titles(query(cat, favorites=True)) == []


async def test_last_launched_reads_newest_ok(cat: Catalog, db: Database) -> None:
    assert cat.last_launched("office") is None
    db.record_launch("office", FAMILY_ONLY, "2026-11-01T10:00:00+00:00", "ok", None)
    db.record_launch("office", GUNSTAR, "2026-11-02T10:00:00+00:00", "ok", None)
    db.record_launch("office", FAMILY_ONLY, "2026-11-03T10:00:00+00:00", "failed", "boom")
    last = cat.last_launched("office")
    assert last is not None and last.game_id == GUNSTAR
    assert last.title == "Gunstar Heroes" and last.at == "2026-11-02T10:00:00+00:00"


async def test_sync_state(cat: Catalog, registry: DeviceRegistry) -> None:
    state = cat.sync_state("office")
    assert state is not None and not state.running and state.status == "ok"
    assert state.finished_at and state.last_good_at == state.finished_at and state.error is None
    assert state.seen == 6
    assert Catalog(Database.memory(), registry, EventHub()).sync_state("office") is None
