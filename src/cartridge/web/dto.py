"""Response DTOs for the web layer (spec 9, 10), built field by field from catalog and registry dataclasses.

Rows are never splatted into a response, so a path, target ref or host can't reach a browser by accident.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from cartridge import __version__
from cartridge.catalog import GameItem, RecentItem, SyncState, SystemItem
from cartridge.core import Core

Mode = Literal["lan", "remote"]


class ErrorDTO(TypedDict):
    code: str
    message: str


class NowPlayingDTO(TypedDict):
    title: str
    system_id: str
    game: str | None
    started_at: str | None


class LastLaunchedDTO(TypedDict):
    game: str
    title: str
    at: str


class SyncDTO(TypedDict):
    running: bool
    status: str | None
    finished_at: str | None
    last_good_at: str | None
    error: str | None
    seen: int
    added: int
    missing: int


class DeviceDTO(TypedDict):
    name: str
    label: str
    model: str
    platform: str
    state: str
    message: str | None
    version: str | None
    now_playing: NowPlayingDTO | None
    last_launched: LastLaunchedDTO | None
    sync: SyncDTO | None


class SyncEventDTO(TypedDict):
    device: str
    status: str | None
    added: int | None  # games new in a finished run; None for started or interrupted runs
    missing: int | None


class SystemDTO(TypedDict):
    id: str
    name: str
    manufacturer: str | None
    game_count: int


class AvailabilityDTO(TypedDict):
    available: bool
    reason: str | None


class GameDTO(TypedDict):
    id: str
    title: str
    system_id: str
    system_name: str
    favorite: bool
    devices: dict[str, AvailabilityDTO]


class GamePageDTO(TypedDict):
    games: list[GameDTO]
    total: int
    offset: int


class RecentDTO(TypedDict):
    device: str
    game: str | None
    title: str
    system_id: str
    started_at: str
    play_time: int
    as_of: str | None


class ConfigDTO(TypedDict):
    mode: Mode
    version: str
    devices_configured: int
    synced: bool


def sync_dto(s: SyncState) -> SyncDTO:
    return SyncDTO(
        running=s.running,
        status=s.status,
        finished_at=s.finished_at,
        last_good_at=s.last_good_at,
        error=s.error,
        seen=s.seen,
        added=s.added,
        missing=s.missing,
    )


def device_dto(core: Core, name: str) -> DeviceDTO:
    """KeyError for a name nobody configured."""
    info = core.registry.info(name)
    if info is None:
        raise KeyError(name)
    status = core.registry.status(name)
    playing = core.catalog.resolve_now_playing(name)
    last = core.catalog.last_launched(name)
    sync = core.catalog.sync_state(name)
    return DeviceDTO(
        name=info.name,
        label=info.label,
        model=info.model,
        platform=info.platform,
        state=status.state,
        message=info.problem if info.problem is not None else status.message,
        version=status.version,
        now_playing=None
        if playing is None
        else NowPlayingDTO(
            title=playing.title, system_id=playing.system_id, game=playing.game_id, started_at=playing.started_at
        ),
        last_launched=None if last is None else LastLaunchedDTO(game=last.game_id, title=last.title, at=last.at),
        sync=None if sync is None else sync_dto(sync),
    )


def devices_dto(core: Core) -> list[DeviceDTO]:
    return [device_dto(core, n) for n in core.registry.names()]


def system_dto(s: SystemItem) -> SystemDTO:
    return SystemDTO(id=s.id, name=s.name, manufacturer=s.manufacturer, game_count=s.game_count)


def game_dto(item: GameItem) -> GameDTO:
    return GameDTO(
        id=item.id,
        title=item.title,
        system_id=item.system_id,
        system_name=item.system_name,
        favorite=item.favorite,
        devices={d: AvailabilityDTO(available=a.available, reason=a.reason) for d, a in item.availability.items()},
    )


def recent_dto(r: RecentItem) -> RecentDTO:
    return RecentDTO(
        device=r.device,
        game=r.game_id,
        title=r.title,
        system_id=r.system_id,
        started_at=r.started_at,
        play_time=r.play_time,
        as_of=r.as_of,
    )


def config_dto(core: Core, mode: Mode) -> ConfigDTO:
    names = core.registry.names()
    return ConfigDTO(
        mode=mode,
        version=__version__,
        devices_configured=sum(1 for n in names if core.registry.target(n) is not None),
        synced=any((s := core.catalog.sync_state(n)) is not None and s.last_good_at is not None for n in names),
    )


def sync_event_dto(event: dict[str, object]) -> SyncEventDTO:
    status = event.get("status")

    def count(key: str) -> int | None:
        value = event.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return SyncEventDTO(
        device=str(event.get("device")),
        status=status if isinstance(status, str) else None,
        added=count("added"),
        missing=count("missing"),
    )
