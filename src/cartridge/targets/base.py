"""The LaunchTarget Protocol (spec 5.1) and the values that cross it.

Everything above the targets (sync, launch flow, web) talks to a device only through this module's
types, so a second kind of device means one new class and no changes upstream. ``TargetRef`` is
opaque outside the target that issued it. Every LaunchError carries a sentence a person can act on
and names the device by its label, never its host.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol

DeviceState = Literal["connecting", "online", "offline", "needs_pairing", "misconfigured"]
EventKind = Literal["status", "media.started", "media.stopped", "indexing.finished"]


@dataclass(frozen=True)
class TargetRef:
    token: str  # the target's own handle for a game (Zaparoo: a mediaId, valid until the next reindex)
    rel_path: str  # the path it was issued for, so the target can tell when the handle went stale


@dataclass(frozen=True)
class NowPlaying:
    system_id: str
    media_name: str
    abs_path: str | None
    started_at: str | None


@dataclass(frozen=True)
class DeviceStatus:
    state: DeviceState
    message: str | None  # None when online; otherwise a sentence saying what is wrong
    version: str | None
    now_playing: NowPlaying | None

    @property
    def online(self) -> bool:
        return self.state == "online"


@dataclass(frozen=True)
class SystemAvailability:
    system_id: str
    name: str
    manufacturer: str | None
    release_date: str | None
    available: bool
    media_count: int


@dataclass(frozen=True)
class CatalogEntry:
    system_id: str
    title: str
    rel_path: str
    abs_path: str
    ref: TargetRef


@dataclass(frozen=True)
class HistoryEntry:
    system_id: str
    media_name: str
    abs_path: str | None
    started_at: str
    play_time: int


@dataclass(frozen=True)
class Image:
    data: bytes
    content_type: str


@dataclass(frozen=True)
class TargetEvent:
    device: str
    kind: EventKind


class TargetError(Exception):
    """A read (systems, catalog, history) that failed; ``str(exc)`` is a sentence naming the device by label."""


class LaunchError(Exception):
    """A launch or return-to-menu that didn't happen; ``str(exc)`` is the sentence for the UI."""

    code: ClassVar[str]
    http_status: ClassVar[int]


class Gone(LaunchError):
    code = "gone"
    http_status = 410


class DeviceBusy(LaunchError):
    code = "device_busy"
    http_status = 409


class Blocked(LaunchError):
    code = "blocked"
    http_status = 409


class LaunchFailed(LaunchError):
    code = "launch_failed"
    http_status = 502

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class LaunchTimeout(LaunchError):
    code = "timeout"
    http_status = 504


class Offline(LaunchError):
    code = "offline"
    http_status = 503


class StaleRef(LaunchError):
    """The ref no longer matches the device's index. The launch flow resyncs and retries; if it escapes, it's 410."""

    code = "gone"
    http_status = 410


class LaunchTarget(Protocol):
    """What a device must do. Failures never leak a transport's own exceptions:

    - ``systems``, ``catalog`` and ``history`` raise ``TargetError`` (a half-read catalog must fail the sync,
      so ``catalog`` raises it mid-iteration too). ``artwork`` returns None on any failure.
    - ``launch`` and ``return_to_menu`` raise ``LaunchError`` subclasses (``StaleRef`` only from ``launch``).
    """

    name: str  # config key, e.g. "family-room"
    model: str
    platform: Literal["mister", "batocera"]

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    def status(self) -> DeviceStatus: ...
    def subscribe(self, listener: Callable[[TargetEvent], None]) -> None: ...

    async def systems(self) -> list[SystemAvailability]: ...
    def catalog(self) -> AsyncIterator[CatalogEntry]: ...
    async def artwork(self, ref: TargetRef, max_size: int) -> Image | None: ...
    async def history(self, limit: int) -> list[HistoryEntry]: ...

    async def launch(self, ref: TargetRef, *, replace: bool = False) -> None: ...
    async def return_to_menu(self) -> None: ...
