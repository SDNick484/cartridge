"""SQLite catalog (spec 6.3): one connection, synchronous, called from the event loop.

Games are never deleted: a game missing from a device's latest sync gets present = 0 there,
and is hidden from listings only when it is present on no device. Timestamps are ISO-8601 UTC
strings supplied by callers, so tests need no clock.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .ids import sort_title

_MIGRATIONS = [
    # version 1
    """
    CREATE TABLE systems (id TEXT PRIMARY KEY, name TEXT NOT NULL, manufacturer TEXT, release_date TEXT);
    CREATE TABLE games (
        id TEXT PRIMARY KEY,
        system_id TEXT NOT NULL REFERENCES systems(id),
        title TEXT NOT NULL,
        sort_title TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX games_sort ON games(sort_title, id);
    CREATE VIRTUAL TABLE games_fts USING fts5(
        title, content='games', content_rowid='rowid', tokenize="unicode61 remove_diacritics 2"
    );
    CREATE TRIGGER games_ai AFTER INSERT ON games BEGIN
        INSERT INTO games_fts(rowid, title) VALUES (new.rowid, new.title);
    END;
    CREATE TRIGGER games_ad AFTER DELETE ON games BEGIN
        INSERT INTO games_fts(games_fts, rowid, title) VALUES ('delete', old.rowid, old.title);
    END;
    CREATE TRIGGER games_au AFTER UPDATE OF title ON games BEGIN
        INSERT INTO games_fts(games_fts, rowid, title) VALUES ('delete', old.rowid, old.title);
        INSERT INTO games_fts(rowid, title) VALUES (new.rowid, new.title);
    END;
    CREATE TABLE device_systems (
        device TEXT NOT NULL,
        system_id TEXT NOT NULL REFERENCES systems(id),
        available INTEGER NOT NULL,
        media_count INTEGER NOT NULL,
        synced_at TEXT NOT NULL,
        PRIMARY KEY (device, system_id)
    );
    CREATE TABLE device_media (
        device TEXT NOT NULL,
        game_id TEXT NOT NULL REFERENCES games(id),
        media_ref TEXT NOT NULL,
        abs_path TEXT NOT NULL,
        rel_path TEXT NOT NULL,
        present INTEGER NOT NULL,
        synced_at TEXT NOT NULL,
        PRIMARY KEY (device, game_id)
    );
    CREATE INDEX device_media_path ON device_media(device, abs_path);
    CREATE TABLE favorites (game_id TEXT PRIMARY KEY REFERENCES games(id), added_at TEXT NOT NULL);
    CREATE TABLE history (
        device TEXT NOT NULL,
        started_at TEXT NOT NULL,
        game_id TEXT,
        system_id TEXT NOT NULL,
        media_name TEXT NOT NULL,
        play_time INTEGER NOT NULL,
        ingested_at TEXT,
        PRIMARY KEY (device, started_at)
    );
    CREATE INDEX history_recent ON history(started_at DESC);
    CREATE TABLE launches (
        id INTEGER PRIMARY KEY,
        device TEXT NOT NULL,
        game_id TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT
    );
    CREATE INDEX launches_device ON launches(device, outcome, requested_at);
    CREATE TABLE art (
        game_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        file TEXT,
        content_type TEXT,
        status TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        retry_after TEXT
    );
    CREATE TABLE sync_runs (
        id INTEGER PRIMARY KEY,
        device TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        seen INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0,
        missing INTEGER NOT NULL DEFAULT 0,
        error TEXT
    );
    """,
]

_PRESENT_ANYWHERE = "EXISTS (SELECT 1 FROM device_media dm WHERE dm.game_id = g.id AND dm.present = 1)"


@dataclass(frozen=True)
class GameRow:
    id: str
    system_id: str
    system_name: str
    title: str
    sort_title: str
    first_seen: str
    updated_at: str


@dataclass(frozen=True)
class SyncRun:
    id: int
    device: str
    started_at: str
    finished_at: str | None
    status: str  # running | ok | failed
    seen: int
    added: int
    missing: int
    error: str | None


@dataclass(frozen=True)
class DeviceMedia:
    device: str
    game_id: str
    media_ref: str
    abs_path: str
    rel_path: str  # server-only: what the target needs to rebuild its ref
    present: bool
    synced_at: str


@dataclass(frozen=True)
class PresenceRow:
    present: bool
    system_available: bool


@dataclass(frozen=True)
class SystemRow:
    id: str
    name: str
    manufacturer: str | None
    release_date: str | None
    game_count: int


@dataclass(frozen=True)
class LaunchRow:
    id: int
    device: str
    game_id: str
    requested_at: str
    outcome: str
    detail: str | None


@dataclass(frozen=True)
class ArtRow:
    game_id: str
    source: str
    file: str | None
    content_type: str | None
    status: str  # found | none
    fetched_at: str
    retry_after: str | None


@dataclass(frozen=True)
class HistoryRow:
    device: str
    started_at: str
    game_id: str | None
    system_id: str
    media_name: str
    play_time: int
    ingested_at: str | None


def fts_query(q: str) -> str | None:
    """Turn user text into a prefix-AND FTS5 query; every token is quoted so syntax can't leak."""
    tokens = [t for t in q.split() if t]
    if not tokens:
        return None
    return " ".join('"' + t.replace('"', '""') + '"*' for t in tokens)


def _marks(n: int) -> str:
    return ",".join("?" * n)


class Database:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    @classmethod
    def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, so transaction() owns BEGIN/COMMIT explicitly.
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        return cls(conn)

    @classmethod
    def memory(cls) -> Database:
        return cls(sqlite3.connect(":memory:", isolation_level=None))

    def close(self) -> None:
        self._conn.close()

    def ping(self) -> bool:
        try:
            self._conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        for n, script in enumerate(_MIGRATIONS, start=1):
            if n <= version:
                continue
            try:
                self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {n};\nCOMMIT;")
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._conn.in_transaction:  # joined: the outermost block commits or rolls back
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- catalog -----------------------------------------------------------------------------

    def upsert_system(self, id: str, name: str, manufacturer: str | None, release_date: str | None) -> None:
        self._conn.execute(
            "INSERT INTO systems(id, name, manufacturer, release_date) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, manufacturer = excluded.manufacturer, "
            "release_date = excluded.release_date",
            (id, name, manufacturer, release_date),
        )

    def ensure_system(self, id: str) -> None:
        """A placeholder for a system a catalog mentions but systems() didn't list; never overwrites a real row."""
        self._conn.execute("INSERT OR IGNORE INTO systems(id, name) VALUES (?, ?)", (id, id))

    def set_device_system(self, device: str, system_id: str, available: bool, media_count: int, synced_at: str) -> None:
        self._conn.execute(
            "INSERT INTO device_systems(device, system_id, available, media_count, synced_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(device, system_id) DO UPDATE SET available = excluded.available, "
            "media_count = excluded.media_count, synced_at = excluded.synced_at",
            (device, system_id, int(available), media_count, synced_at),
        )

    def upsert_game(self, game_id: str, system_id: str, title: str, now: str) -> bool:
        """Insert or refresh a game; True when it is new. `first_seen` never changes."""
        new = self._conn.execute("SELECT 1 FROM games WHERE id = ?", (game_id,)).fetchone() is None
        self._conn.execute(
            "INSERT INTO games(id, system_id, title, sort_title, first_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, sort_title = excluded.sort_title, "
            "updated_at = excluded.updated_at",
            (game_id, system_id, title, sort_title(title), now, now),
        )
        return new

    def upsert_device_media(
        self, device: str, game_id: str, media_ref: str, abs_path: str, rel_path: str, synced_at: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO device_media(device, game_id, media_ref, abs_path, rel_path, present, synced_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT(device, game_id) DO UPDATE SET media_ref = excluded.media_ref, "
            "abs_path = excluded.abs_path, rel_path = excluded.rel_path, present = 1, synced_at = excluded.synced_at",
            (device, game_id, media_ref, abs_path, rel_path, synced_at),
        )

    def mark_missing(self, device: str, run_stamp: str) -> int:
        """Flip present rows not stamped with this run's `run_stamp` to 0; returns how many flipped.

        Equality, not ordering: every row a run touches carries exactly its stamp, so a clock that
        stepped backwards between runs can't leave unseen rows marked present.
        """
        cur = self._conn.execute(
            "UPDATE device_media SET present = 0 WHERE device = ? AND present = 1 AND synced_at <> ?",
            (device, run_stamp),
        )
        return cur.rowcount

    # -- sync runs ---------------------------------------------------------------------------

    def start_sync_run(self, device: str, started_at: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO sync_runs(device, started_at, status) VALUES (?, ?, 'running')", (device, started_at)
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_sync_run(
        self,
        run_id: int,
        finished_at: str,
        status: Literal["ok", "failed"],
        seen: int,
        added: int,
        missing: int,
        error: str | None,
    ) -> None:
        self._conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, seen = ?, added = ?, missing = ?, error = ? "
            "WHERE id = ?",
            (finished_at, status, seen, added, missing, error, run_id),
        )

    def last_sync(self, device: str) -> SyncRun | None:
        return self._sync_run("WHERE device = ?", (device,))

    def last_good_sync(self, device: str) -> SyncRun | None:
        return self._sync_run("WHERE device = ? AND status = 'ok'", (device,))

    def _sync_run(self, where: str, params: tuple[str, ...]) -> SyncRun | None:
        r = self._conn.execute(f"SELECT * FROM sync_runs {where} ORDER BY id DESC LIMIT 1", params).fetchone()
        return None if r is None else SyncRun(**dict(r))

    # -- lookups -----------------------------------------------------------------------------

    def device_media(self, device: str, game_id: str) -> DeviceMedia | None:
        r = self._conn.execute(
            "SELECT * FROM device_media WHERE device = ? AND game_id = ?", (device, game_id)
        ).fetchone()
        return None if r is None else DeviceMedia(**{**dict(r), "present": bool(r["present"])})

    def game_id_for_path(self, device: str, abs_path: str) -> str | None:
        r = self._conn.execute(
            "SELECT game_id FROM device_media WHERE device = ? AND abs_path = ? ORDER BY present DESC LIMIT 1",
            (device, abs_path),
        ).fetchone()
        return None if r is None else str(r["game_id"])

    _GAME_SELECT = (
        "SELECT g.id, g.system_id, s.name AS system_name, g.title, g.sort_title, g.first_seen, g.updated_at "
        "FROM games g JOIN systems s ON s.id = g.system_id"
    )

    def game(self, game_id: str) -> GameRow | None:
        r = self._conn.execute(f"{self._GAME_SELECT} WHERE g.id = ?", (game_id,)).fetchone()
        return None if r is None else GameRow(**dict(r))

    def query_games(
        self,
        *,
        system: str | None,
        q: str | None,
        present_on: str | None,
        favorites: bool,
        include_unavailable: bool,
        offset: int,
        limit: int,
    ) -> tuple[list[GameRow], int]:
        where: list[str] = []
        params: list[object] = []
        if system is not None:
            where.append("g.system_id = ?")
            params.append(system)
        match = fts_query(q) if q is not None else None
        if match is not None:
            where.append("g.rowid IN (SELECT rowid FROM games_fts WHERE games_fts MATCH ?)")
            params.append(match)
        if present_on is not None:
            where.append(
                "EXISTS (SELECT 1 FROM device_media dm WHERE dm.game_id = g.id AND dm.device = ? AND dm.present = 1)"
            )
            params.append(present_on)
        elif not include_unavailable:
            where.append(_PRESENT_ANYWHERE)
        if favorites:
            where.append("g.id IN (SELECT game_id FROM favorites)")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(self._conn.execute(f"SELECT COUNT(*) FROM games g{clause}", params).fetchone()[0])
        rows = self._conn.execute(
            f"{self._GAME_SELECT}{clause} ORDER BY g.sort_title, g.id LIMIT ? OFFSET ?", [*params, limit, offset]
        ).fetchall()
        return [GameRow(**dict(r)) for r in rows], total

    def presence(self, game_ids: Sequence[str]) -> dict[str, dict[str, PresenceRow]]:
        out: dict[str, dict[str, PresenceRow]] = {}
        if not game_ids:
            return out
        rows = self._conn.execute(
            "SELECT dm.game_id, dm.device, dm.present, COALESCE(ds.available, 0) AS available "
            "FROM device_media dm JOIN games g ON g.id = dm.game_id "
            "LEFT JOIN device_systems ds ON ds.device = dm.device AND ds.system_id = g.system_id "
            f"WHERE dm.game_id IN ({_marks(len(game_ids))})",
            list(game_ids),
        ).fetchall()
        for r in rows:
            out.setdefault(r["game_id"], {})[r["device"]] = PresenceRow(bool(r["present"]), bool(r["available"]))
        return out

    def systems_with_games(self) -> list[SystemRow]:
        rows = self._conn.execute(
            "SELECT s.id, s.name, s.manufacturer, s.release_date, COUNT(g.id) AS game_count "
            f"FROM systems s JOIN games g ON g.system_id = s.id AND {_PRESENT_ANYWHERE} "
            "GROUP BY s.id ORDER BY s.name, s.id"
        ).fetchall()
        return [SystemRow(**dict(r)) for r in rows]

    # -- favorites, history, launches, art ---------------------------------------------------

    def set_favorite(self, game_id: str, on: bool, now: str) -> None:
        if on:
            self._conn.execute("INSERT OR IGNORE INTO favorites(game_id, added_at) VALUES (?, ?)", (game_id, now))
        else:
            self._conn.execute("DELETE FROM favorites WHERE game_id = ?", (game_id,))

    def favorite_ids(self, game_ids: Sequence[str]) -> set[str]:
        if not game_ids:
            return set()
        rows = self._conn.execute(
            f"SELECT game_id FROM favorites WHERE game_id IN ({_marks(len(game_ids))})", list(game_ids)
        ).fetchall()
        return {r["game_id"] for r in rows}

    def upsert_history(self, device: str, entries: Sequence[HistoryRow], ingested_at: str) -> None:
        """Cache media.history rows; `ingested_at` backs the 'as of' time for an offline device."""
        with self.transaction():
            for e in entries:
                self._conn.execute(
                    "INSERT INTO history(device, started_at, game_id, system_id, media_name, play_time, ingested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(device, started_at) DO UPDATE SET "
                    "game_id = excluded.game_id, system_id = excluded.system_id, media_name = excluded.media_name, "
                    "play_time = excluded.play_time, ingested_at = excluded.ingested_at",
                    (device, e.started_at, e.game_id, e.system_id, e.media_name, e.play_time, ingested_at),
                )

    def recent_history(self, limit: int) -> list[HistoryRow]:
        rows = self._conn.execute(
            "SELECT device, started_at, game_id, system_id, media_name, play_time, ingested_at FROM history "
            "ORDER BY started_at DESC, device LIMIT ?",
            (limit,),
        ).fetchall()
        return [HistoryRow(**dict(r)) for r in rows]

    def history_as_of(self, device: str) -> str | None:
        r = self._conn.execute("SELECT MAX(ingested_at) FROM history WHERE device = ?", (device,)).fetchone()
        return None if r[0] is None else str(r[0])

    def record_launch(self, device: str, game_id: str, requested_at: str, outcome: str, detail: str | None) -> None:
        self._conn.execute(
            "INSERT INTO launches(device, game_id, requested_at, outcome, detail) VALUES (?, ?, ?, ?, ?)",
            (device, game_id, requested_at, outcome, detail),
        )

    def last_ok_launch(self, device: str) -> LaunchRow | None:
        r = self._conn.execute(
            "SELECT * FROM launches WHERE device = ? AND outcome = 'ok' ORDER BY requested_at DESC, id DESC LIMIT 1",
            (device,),
        ).fetchone()
        return None if r is None else LaunchRow(**dict(r))

    def art(self, game_id: str) -> ArtRow | None:
        r = self._conn.execute("SELECT * FROM art WHERE game_id = ?", (game_id,)).fetchone()
        return None if r is None else ArtRow(**dict(r))

    def set_art(
        self,
        game_id: str,
        source: str,
        file: str | None,
        content_type: str | None,
        status: Literal["found", "none"],
        fetched_at: str,
        retry_after: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO art(game_id, source, file, content_type, status, fetched_at, retry_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(game_id) DO UPDATE SET source = excluded.source, "
            "file = excluded.file, content_type = excluded.content_type, status = excluded.status, "
            "fetched_at = excluded.fetched_at, retry_after = excluded.retry_after",
            (game_id, source, file, content_type, status, fetched_at, retry_after),
        )
