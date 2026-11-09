from pathlib import Path

import pytest

from cartridge.db import Database, HistoryRow, fts_query

T0 = "2026-11-07T10:00:00Z"
T1 = "2026-11-07T11:00:00Z"
T2 = "2026-11-07T12:00:00Z"


@pytest.fixture
def db() -> Database:
    d = Database.memory()
    d.upsert_system("snes", "Super Nintendo", "Nintendo", "1990-11-21")
    d.upsert_system("genesis", "Genesis", "Sega", None)
    return d


def add(db: Database, gid: str, title: str, system: str = "snes", device: str | None = "family-room") -> None:
    db.upsert_game(gid, system, title, T0)
    if device:
        db.upsert_device_media(device, gid, f"ref-{gid}", f"/media/{gid}.sfc", T0)


def titles(db: Database, **kw: object) -> list[str]:
    args: dict[str, object] = {
        "system": None,
        "q": None,
        "present_on": None,
        "favorites": False,
        "include_unavailable": False,
        "offset": 0,
        "limit": 100,
    }
    args.update(kw)
    rows, _total = db.query_games(**args)  # type: ignore[arg-type]
    return [r.title for r in rows]


def test_migrates_to_version_1(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "c.db"
    d = Database.open(path)
    assert d.ping()
    assert d._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert d._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert d._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    d.upsert_system("snes", "Super Nintendo", None, None)
    d.close()
    again = Database.open(path)  # would raise "table already exists" if migrations re-ran
    assert again._conn.execute("SELECT id FROM systems").fetchone()[0] == "snes"
    again.close()


def test_upsert_game_reports_new_once(db: Database) -> None:
    assert db.upsert_game("g1", "snes", "The Game", T0) is True
    assert db.upsert_game("g1", "snes", "Game, The Renamed", T1) is False
    g = db.game("g1")
    assert g is not None
    assert g.first_seen == T0
    assert g.updated_at == T1
    assert g.title == "Game, The Renamed"
    assert g.sort_title == "game, the renamed"
    assert g.system_name == "Super Nintendo"
    assert db.game("nope") is None


def test_mark_missing_only_flips_unseen_on_that_device(db: Database) -> None:
    add(db, "g1", "One")
    add(db, "g2", "Two")
    db.upsert_device_media("office", "g1", "r", "/o/g1", T0)
    db.upsert_device_media("family-room", "g1", "r2", "/media/g1.sfc", T2)  # seen in the new run
    assert db.mark_missing("family-room", T2) == 1
    assert db.device_media("family-room", "g1").present is True  # type: ignore[union-attr]
    assert db.device_media("family-room", "g2").present is False  # type: ignore[union-attr]
    assert db.device_media("office", "g1").present is True  # type: ignore[union-attr]
    assert db.mark_missing("family-room", T2) == 0
    db.upsert_device_media("family-room", "g2", "r3", "/media/g2.sfc", T2)
    assert db.device_media("family-room", "g2").present is True  # type: ignore[union-attr]
    assert db.game_id_for_path("family-room", "/media/g2.sfc") == "g2"
    assert db.game_id_for_path("family-room", "/nope") is None
    assert db.device_media("family-room", "zz") is None


def test_query_hides_games_present_nowhere_unless_asked(db: Database) -> None:
    add(db, "g1", "Here")
    add(db, "g2", "Gone")
    db.mark_missing("family-room", T1)
    db.upsert_device_media("family-room", "g1", "r", "/p", T2)
    assert titles(db) == ["Here"]
    assert titles(db, include_unavailable=True) == ["Gone", "Here"]
    assert [s.id for s in db.systems_with_games()] == ["snes"]
    db.mark_missing("family-room", T2 + "z")
    assert db.systems_with_games() == []


def test_query_present_on_and_system_filters(db: Database) -> None:
    add(db, "g1", "Alpha")
    add(db, "g2", "Beta", device="office")
    add(db, "g3", "Charlie", system="genesis")
    add(db, "g4", "Delta")
    add(db, "g5", "Echo")
    assert titles(db, present_on="office") == ["Beta"]
    assert titles(db, system="genesis") == ["Charlie"]
    rows, total = db.query_games(
        system=None, q=None, present_on=None, favorites=False, include_unavailable=False, offset=1, limit=2
    )
    assert total == 5
    assert [r.title for r in rows] == ["Beta", "Charlie"]
    assert [r.system_name for r in rows] == ["Super Nintendo", "Genesis"]
    systems = {s.id: s.game_count for s in db.systems_with_games()}
    assert systems == {"snes": 4, "genesis": 1}


def test_query_orders_by_sort_title_then_id(db: Database) -> None:
    add(db, "b", "The Same")
    add(db, "a", "Same")
    add(db, "c", "Zed")
    rows, _ = db.query_games(
        system=None, q=None, present_on=None, favorites=False, include_unavailable=False, offset=0, limit=10
    )
    assert [r.id for r in rows] == ["a", "b", "c"]


def test_search_matches_accents_and_prefix(db: Database) -> None:
    add(db, "g1", "Façade & Friends: Part II")
    add(db, "g2", "Other")
    for q in ("facade", "fri", "Part II", "  FAÇADE  part "):
        assert titles(db, q=q) == ["Façade & Friends: Part II"], q
    assert titles(db, q="zzz") == []
    assert titles(db, q="   ") == ["Façade & Friends: Part II", "Other"]
    db.upsert_game("g1", "snes", "Renamed", T1)
    assert titles(db, q="facade") == []
    assert titles(db, q="renam") == ["Renamed"]


@pytest.mark.parametrize("q", ['"', "*", "AND", "c:", "NEAR(", "'", "-", '"a" OR', "a*b"])
def test_search_survives_fts_syntax(db: Database, q: str) -> None:
    add(db, "g1", "Alpha")
    titles(db, q=q)  # must not raise


def test_fts_query() -> None:
    assert fts_query('a "b') == '"a"* """b"*'
    assert fts_query("  ") is None
    assert fts_query("") is None


def test_filters_favorites(db: Database) -> None:
    add(db, "g1", "Alpha")
    add(db, "g2", "Beta")
    db.set_favorite("g2", True, T0)
    assert titles(db, favorites=True) == ["Beta"]


def test_transaction_rolls_back(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction():
        db.upsert_system("nes", "NES", None, None)
        raise RuntimeError("boom")
    assert db._conn.execute("SELECT COUNT(*) FROM systems WHERE id = 'nes'").fetchone()[0] == 0
    with db.transaction():
        db.upsert_system("nes", "NES", None, None)
    assert db._conn.execute("SELECT COUNT(*) FROM systems WHERE id = 'nes'").fetchone()[0] == 1


def test_presence_and_device_systems(db: Database) -> None:
    add(db, "g1", "Alpha")
    db.upsert_device_media("office", "g1", "r", "/o", T0)
    db.set_device_system("family-room", "snes", True, 10, T0)
    db.set_device_system("office", "snes", True, 3, T0)
    db.set_device_system("office", "snes", False, 0, T1)  # upsert
    db.mark_missing("office", T2)
    p = db.presence(["g1", "zz"])
    assert set(p) == {"g1"}
    assert p["g1"]["family-room"].present is True
    assert p["g1"]["family-room"].system_available is True
    assert p["g1"]["office"].present is False
    assert p["g1"]["office"].system_available is False
    assert db.presence([]) == {}


def test_favorites_roundtrip(db: Database) -> None:
    add(db, "g1", "Alpha")
    add(db, "g2", "Beta")
    db.set_favorite("g1", True, T0)
    db.set_favorite("g1", True, T1)  # idempotent
    assert db.favorite_ids(["g1", "g2"]) == {"g1"}
    db.set_favorite("g1", False, T2)
    assert db.favorite_ids(["g1", "g2"]) == set()
    assert db.favorite_ids([]) == set()


def test_recent_history_merges_devices_newest_first(db: Database) -> None:
    add(db, "g1", "Alpha")

    def row(device: str, started: str, game: str | None, name: str, play: int = 5) -> HistoryRow:
        return HistoryRow(device, started, game, "snes", name, play, None)

    db.upsert_history(
        "family-room", [row("family-room", T0, "g1", "Alpha"), row("family-room", T2, None, "Menu Pick")], T2
    )
    db.upsert_history("office", [row("office", T1, "g1", "Alpha")], T1)
    db.upsert_history("office", [row("office", T1, "g1", "Alpha", play=9)], T2 + "x")  # same key: updated
    got = db.recent_history(10)
    assert [(h.device, h.started_at) for h in got] == [("family-room", T2), ("office", T1), ("family-room", T0)]
    assert got[0].game_id is None
    assert got[1].play_time == 9
    assert [h.device for h in db.recent_history(2)] == ["family-room", "office"]
    assert db.history_as_of("office") == T2 + "x"
    assert db.history_as_of("family-room") == T2
    assert db.history_as_of("nobody") is None


def test_last_ok_launch_ignores_failures(db: Database) -> None:
    add(db, "g1", "Alpha")
    add(db, "g2", "Beta")
    assert db.last_ok_launch("family-room") is None
    db.record_launch("family-room", "g1", T0, "ok", None)
    db.record_launch("family-room", "g2", T1, "ok", None)
    db.record_launch("family-room", "g1", T2, "timeout", "no answer")
    db.record_launch("office", "g1", T2, "ok", None)
    last = db.last_ok_launch("family-room")
    assert last is not None
    assert (last.game_id, last.requested_at, last.outcome) == ("g2", T1, "ok")


def test_sync_runs(db: Database) -> None:
    assert db.last_sync("family-room") is None
    r1 = db.start_sync_run("family-room", T0)
    db.finish_sync_run(r1, T1, "ok", 10, 4, 1, None)
    r2 = db.start_sync_run("family-room", T1)
    db.finish_sync_run(r2, T2, "failed", 0, 0, 0, "No answer from Family Room.")
    r3 = db.start_sync_run("family-room", T2)
    last = db.last_sync("family-room")
    assert last is not None and last.id == r3 and last.status == "running" and last.finished_at is None
    db.finish_sync_run(r3, T2, "failed", 0, 0, 0, "x")
    last = db.last_sync("family-room")
    assert last is not None and last.error == "x"
    good = db.last_good_sync("family-room")
    assert good is not None
    assert (good.id, good.status, good.seen, good.added, good.missing) == (r1, "ok", 10, 4, 1)
    assert db.last_good_sync("office") is None


def test_art_roundtrip(db: Database) -> None:
    add(db, "g1", "Alpha")
    assert db.art("g1") is None
    db.set_art("g1", "libretro", None, None, "none", T0, T2)
    a = db.art("g1")
    assert a is not None and a.status == "none" and a.retry_after == T2 and a.file is None
    db.set_art("g1", "zaparoo", "ab/g1.png", "image/png", "found", T1, None)
    a = db.art("g1")
    assert a is not None and (a.source, a.file, a.content_type, a.status, a.retry_after) == (
        "zaparoo",
        "ab/g1.png",
        "image/png",
        "found",
        None,
    )
