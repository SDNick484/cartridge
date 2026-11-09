"""Artwork chain, cache and placeholder (spec 8): Zaparoo ``media.image``, then libretro-thumbnails, then a tile.

A tile is never stored as "found". A miss (``status none``) is recorded only when at least one source
answered "no art" and none was unable to tell (rate limited, network error), so a transient failure is
retried on the next request. While a miss is valid (7 days) the placeholder is served without any fetch.
The cache file's path is stored relative to ``art_dir`` and never leaves this module. One resolution per
game id runs at a time; libretro gets one request per second and is paused after a 429.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

import anyio
import httpx

from cartridge.catalog import Catalog, utcnow
from cartridge.db import Database
from cartridge.ratelimit import TokenBucket
from cartridge.registry import DeviceRegistry
from cartridge.targets.base import TargetRef

log = logging.getLogger(__name__)

LIBRETRO_BASE = "https://thumbnails.libretro.com"
MAX_SIZE = 512
MAX_BYTES = 5 * 1024 * 1024
MISS_DAYS = 7
DEFAULT_PAUSE = 60.0
PREFETCH_CONCURRENCY = 2
REQUEST_TIMEOUT = 10.0

EXTENSIONS = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg"}

PLAYLISTS: dict[str, str] = {
    "SNES": "Nintendo - Super Nintendo Entertainment System",
    "Genesis": "Sega - Mega Drive - Genesis",
    "NES": "Nintendo - Nintendo Entertainment System",
    "GBA": "Nintendo - Game Boy Advance",
    "Gameboy": "Nintendo - Game Boy",
    "GameboyColor": "Nintendo - Game Boy Color",
    "MasterSystem": "Sega - Master System - Mark III",
    "GameGear": "Sega - Game Gear",
    "TurboGrafx16": "NEC - PC Engine - TurboGrafx 16",
    "PSX": "Sony - PlayStation",
    "Nintendo64": "Nintendo - Nintendo 64",
    "Atari2600": "Atari - 2600",
    "NeoGeo": "SNK - Neo Geo",
}

# Dark enough that white text clears 4.5:1 (a test computes it for every entry).
DEFAULT_COLOR = "#374151"
SYSTEM_COLORS: dict[str, str] = {
    "SNES": "#4b3f8f",
    "Genesis": "#1f4e79",
    "NES": "#8a2b2b",
    "GBA": "#4a3a8a",
    "Gameboy": "#3f5a2a",
    "GameboyColor": "#6b2f7a",
    "MasterSystem": "#8a2f2f",
    "GameGear": "#1c4a5a",
    "TurboGrafx16": "#7a3b12",
    "PSX": "#3d4451",
    "Nintendo64": "#2a6b3a",
    "Atari2600": "#6b4a1c",
    "NeoGeo": "#7a1f3d",
}

_RESERVED = re.compile(r'[&*/:`<>?\\|"]')


def libretro_url(system_id: str, rel_path: str) -> str | None:
    playlist = PLAYLISTS.get(system_id)
    base = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    # ASSUMPTION L-THUMB-URL: Named_Boxarts/<file name without extension, reserved characters as _>.png
    name = _RESERVED.sub("_", os.path.splitext(base)[0])
    if playlist is None or not name:
        return None
    return f"{LIBRETRO_BASE}/{urllib.parse.quote(playlist)}/Named_Boxarts/{urllib.parse.quote(name)}.png"


def _wrap(title: str, width: int = 14, max_lines: int = 5) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in (w[:width] for w in title.split()):
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[max_lines - 1] + "…"]
    return lines or [""]


def placeholder_svg(title: str, system_id: str) -> bytes:
    color = SYSTEM_COLORS.get(system_id, DEFAULT_COLOR)
    lines = _wrap(title)
    top = 200 - (len(lines) - 1) * 18
    text = "".join(f'<tspan x="150" y="{top + i * 36}">{escape(line)}</tspan>' for i, line in enumerate(lines))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 400" role="img">'
        f'<rect width="300" height="400" fill="{color}"/>'
        '<text fill="#ffffff" font-family="sans-serif" font-size="28" font-weight="600" '
        f'text-anchor="middle">{text}</text></svg>'
    )
    return svg.encode("utf-8")


@dataclass(frozen=True)
class ArtResult:
    body: bytes
    content_type: str
    etag: str
    found: bool


def _result(body: bytes, content_type: str, found: bool) -> ArtResult:
    return ArtResult(body, content_type, '"' + hashlib.sha256(body).hexdigest()[:16] + '"', found)


@dataclass(frozen=True)
class _Art:
    data: bytes
    content_type: str


# What a source says: art, "no art here" (a miss counts), "couldn't tell" (no miss), or doesn't apply.
_NoArt = Literal["none"]
_Unknown = Literal["unknown"]
_Outcome = _Art | _NoArt | _Unknown | None


class ArtCache:
    def __init__(
        self,
        db: Database,
        registry: DeviceRegistry,
        catalog: Catalog,
        art_dir: Path,
        *,
        http: httpx.AsyncClient,
        online: bool,
        version: str,
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self.db = db
        self.registry = registry
        self.catalog = catalog
        self.art_dir = art_dir
        self._http = http  # the caller owns it and closes it
        self._online = online
        self._ua = f"cartridge/{version}"
        self._now = now
        self._clock = clock
        self._bucket = TokenBucket(capacity=1, rate=1, clock=clock, sleep=sleep)
        self._paused_until = 0.0
        self._inflight: dict[str, asyncio.Task[ArtResult]] = {}

    def _stamp(self, at: datetime | None = None) -> str:
        return (at or self._now()).isoformat(timespec="microseconds")

    async def get(self, game_id: str) -> ArtResult | None:
        """Art for a game, or its placeholder; None for an unknown game."""
        if self.db.game(game_id) is None:
            return None
        task = self._inflight.get(game_id)
        if task is None:
            task = asyncio.create_task(self._resolve(game_id))
            self._inflight[game_id] = task
            task.add_done_callback(lambda _: self._inflight.pop(game_id, None))
        return await asyncio.shield(task)

    async def prefetch(self, game_ids: Iterable[str]) -> None:
        gate = asyncio.Semaphore(PREFETCH_CONCURRENCY)

        async def one(game_id: str) -> None:
            async with gate:
                try:
                    if self._needs_fetch(game_id):
                        await self.get(game_id)
                except Exception as exc:
                    log.warning("Art prefetch failed for %s: %s", game_id, exc)

        await asyncio.gather(*(one(g) for g in game_ids))

    async def close(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _needs_fetch(self, game_id: str) -> bool:
        row = self.db.art(game_id)
        if row is None:
            return True
        if row.status == "found":
            return not (row.file and (self.art_dir / row.file).is_file())
        return row.retry_after is None or row.retry_after <= self._stamp()

    async def _resolve(self, game_id: str) -> ArtResult:
        game = self.db.game(game_id)
        assert game is not None
        row = self.db.art(game_id)
        if row is not None and row.status == "found" and row.file and row.content_type:
            try:
                return _result((self.art_dir / row.file).read_bytes(), row.content_type, True)
            except OSError:
                pass  # the file is gone: fetch again
        elif row is not None and row.status == "none" and row.retry_after and row.retry_after > self._stamp():
            return self._placeholder(game.title, game.system_id)

        outcomes: list[_Outcome] = []
        for source, fetch in (("zaparoo", self._from_device), ("libretro", self._from_libretro)):
            outcome = await fetch(game_id, game.system_id)
            if isinstance(outcome, _Art):
                return self._store(game_id, outcome, source)
            outcomes.append(outcome)
        if "none" in outcomes and "unknown" not in outcomes:
            now = self._now()
            self.db.set_art(
                game_id, "none", None, None, "none", self._stamp(now), self._stamp(now + timedelta(days=MISS_DAYS))
            )
        return self._placeholder(game.title, game.system_id)

    @staticmethod
    def _placeholder(title: str, system_id: str) -> ArtResult:
        return _result(placeholder_svg(title, system_id), "image/svg+xml", False)

    def _store(self, game_id: str, art: _Art, source: str) -> ArtResult:
        rel = f"{game_id[:2]}/{game_id}.{EXTENSIONS[art.content_type]}"
        path = self.art_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(art.data)
        tmp.replace(path)
        self.db.set_art(game_id, source, rel, art.content_type, "found", self._stamp(), None)
        return _result(art.data, art.content_type, True)

    async def _from_device(self, game_id: str, system_id: str) -> _Outcome:
        asked = False
        for name in self.registry.names():
            target = self.registry.target(name)
            media = self.db.device_media(name, game_id)
            if target is None or media is None or not media.present or not self.registry.status(name).online:
                continue
            asked = True
            image = await target.artwork(TargetRef(media.media_ref, media.rel_path), MAX_SIZE)
            if image is not None and image.content_type in EXTENSIONS and 0 < len(image.data) <= MAX_BYTES:
                return _Art(image.data, image.content_type)
        return "none" if asked else None

    async def _from_libretro(self, game_id: str, system_id: str) -> _Outcome:
        if not self._online:
            return None
        rel_path = self._any_rel_path(game_id)
        url = libretro_url(system_id, rel_path) if rel_path else None
        if url is None:
            return None
        if self._clock() < self._paused_until:
            return "unknown"
        await self._bucket.acquire()
        try:
            resp = await self._http.get(
                url, headers={"User-Agent": self._ua}, follow_redirects=False, timeout=REQUEST_TIMEOUT
            )
        except httpx.HTTPError as exc:
            log.warning("libretro request failed: %s", type(exc).__name__)
            return "unknown"
        if resp.status_code == 404:
            return "none"
        if resp.status_code == 429:
            self._paused_until = self._clock() + _retry_after(resp)
            return "unknown"
        if resp.status_code != 200:
            return "unknown"
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in EXTENSIONS or not 0 < len(resp.content) <= MAX_BYTES:
            return "none"
        return _Art(resp.content, content_type)

    def _any_rel_path(self, game_id: str) -> str | None:
        rows = [m for n in self.registry.names() if (m := self.db.device_media(n, game_id)) is not None]
        rows.sort(key=lambda m: not m.present)
        return rows[0].rel_path if rows else None


def _retry_after(resp: httpx.Response) -> float:
    try:
        seconds = float(resp.headers.get("retry-after", ""))
    except ValueError:
        return DEFAULT_PAUSE
    return seconds if 0 <= seconds < float("inf") else DEFAULT_PAUSE
