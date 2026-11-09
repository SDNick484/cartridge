import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cartridge.art import DEFAULT_COLOR, PLAYLISTS, SYSTEM_COLORS, ArtCache, libretro_url, placeholder_svg
from cartridge.catalog import Catalog
from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.ids import game_id
from cartridge.registry import DeviceRegistry
from cartridge.sim.fake_zaparoo import tiny_png
from cartridge.sim.harness import Simulation
from cartridge.targets.base import DeviceStatus

pytestmark = pytest.mark.anyio

METROID = game_id("SNES", "Super Metroid (USA).sfc")
DKC = game_id("SNES", "Donkey Kong Country (USA).sfc")
FACADE = game_id("SNES", "Façade & Friends - Part II (USA).sfc")
GUNSTAR = game_id("Genesis", "Gunstar Heroes (USA).md")
DKC_URL = (
    "https://thumbnails.libretro.com/Nintendo%20-%20Super%20Nintendo%20Entertainment%20System/"
    "Named_Boxarts/Donkey%20Kong%20Country%20%28USA%29.png"
)
T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


class Clock:
    """One fake time for both the datetime provider and the monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.at = T0

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.at += timedelta(seconds=seconds)

    def now(self) -> datetime:
        return self.at

    def mono(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class Net:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(404)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "thumbnails.libretro.com", f"unexpected host {request.url.host}"
        self.requests.append(request)
        return self.respond(request)


@pytest.fixture
def net() -> Net:
    return Net()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def art(
    sim: Simulation, db: Database, registry: DeviceRegistry, net: Net, clock: Clock, tmp_path: Path
) -> ArtCache:
    cat = Catalog(db, registry, EventHub())
    await cat.sync("family-room")
    await cat.sync("office")
    http = httpx.AsyncClient(transport=httpx.MockTransport(net.handler))
    cache = ArtCache(
        db,
        registry,
        cat,
        tmp_path / "art",
        http=http,
        online=True,
        version="0.1.0",
        now=clock.now,
        clock=clock.mono,
        sleep=clock.sleep,
    )
    yield cache  # type: ignore[misc]
    await cache.close()
    await http.aclose()


def png_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=tiny_png(), headers={"content-type": "image/png"})


async def test_zaparoo_image_first(
    art: ArtCache, net: Net, db: Database, registry: DeviceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    target = registry.target("family-room")
    assert target is not None
    original = target.artwork

    async def counting(ref, max_size):  # type: ignore[no-untyped-def]
        calls.append(ref.token)
        return await original(ref, max_size)

    monkeypatch.setattr(target, "artwork", counting)

    first = await art.get(METROID)
    assert first is not None
    assert first.found and first.content_type == "image/png" and first.body == tiny_png()
    assert net.requests == []
    row = db.art(METROID)
    assert row is not None and row.status == "found" and row.source == "zaparoo"
    assert row.file == f"{METROID[:2]}/{METROID}.png"
    assert (art.art_dir / row.file).is_file()

    second = await art.get(METROID)
    assert second == first
    assert len(calls) == 1


async def test_libretro_second(art: ArtCache, net: Net, db: Database) -> None:
    net.respond = png_response
    got = await art.get(DKC)
    assert got is not None and got.found and got.content_type == "image/png"
    assert [str(r.url) for r in net.requests] == [DKC_URL]
    assert net.requests[0].headers["user-agent"] == "cartridge/0.1.0"
    row = db.art(DKC)
    assert row is not None and row.source == "libretro"
    assert re.fullmatch(r'"[0-9a-f]{16}"', got.etag)


async def test_libretro_name_replacement(art: ArtCache, net: Net) -> None:
    net.respond = png_response
    await art.get(FACADE)
    assert len(net.requests) == 1
    assert net.requests[0].url.raw_path.endswith(
        b"/Named_Boxarts/Fa%C3%A7ade%20_%20Friends%20-%20Part%20II%20%28USA%29.png"
    )


def test_libretro_url_replaces_every_reserved_character() -> None:
    url = libretro_url("SNES", 'dir/a&b*c:d`e<f>g?h|i"j.sfc')
    assert url is not None and url.endswith("/Named_Boxarts/a_b_c_d_e_f_g_h_i_j.png")
    assert libretro_url("Nope", "x.bin") is None


async def test_miss_is_cached_for_7_days(art: ArtCache, net: Net, db: Database, clock: Clock) -> None:
    got = await art.get(DKC)
    assert got is not None and not got.found and got.content_type == "image/svg+xml"
    assert not got.transient  # a recorded miss
    row = db.art(DKC)
    assert row is not None and row.status == "none" and row.file is None
    assert row.retry_after == (T0 + timedelta(days=7)).isoformat(timespec="microseconds")
    assert len(net.requests) == 1

    await art.get(DKC)
    assert len(net.requests) == 1

    clock.advance(8 * 86400)
    await art.get(DKC)
    assert len(net.requests) == 2


async def test_device_connection_loss_is_transient(art: ArtCache, sim: Simulation, db: Database) -> None:
    for dev in sim.devices:
        dev.fake.close_after = "media.image"  # the socket drops mid-request, unanswered
    got = await art.get(METROID)
    assert got is not None and not got.found and got.transient
    assert db.art(METROID) is None  # no 7-day miss for a device that couldn't answer


async def test_retry_after_honored(art: ArtCache, net: Net, db: Database, clock: Clock) -> None:
    net.respond = lambda r: httpx.Response(429, headers={"retry-after": "120"})
    got = await art.get(DKC)
    assert got is not None and not got.found
    assert db.art(DKC) is None
    assert len(net.requests) == 1

    clock.advance(60)
    await art.get(DKC)
    assert len(net.requests) == 1

    clock.advance(61)
    await art.get(DKC)
    assert len(net.requests) == 2


async def test_rate_limited_without_retry_after_pauses_60s(art: ArtCache, net: Net, clock: Clock) -> None:
    net.respond = lambda r: httpx.Response(429)
    await art.get(DKC)
    clock.advance(59)
    await art.get(DKC)
    assert len(net.requests) == 1
    clock.advance(2)
    await art.get(DKC)
    assert len(net.requests) == 2


async def test_requests_are_paced_one_per_second(art: ArtCache, net: Net, clock: Clock) -> None:
    stamps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        stamps.append(clock.t)
        return httpx.Response(404)

    net.respond = respond
    await art.get(DKC)
    await art.get(FACADE)
    assert stamps[1] - stamps[0] >= 1.0


async def test_network_error_records_no_miss(art: ArtCache, net: Net, db: Database) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    net.respond = boom
    got = await art.get(DKC)
    assert got is not None and not got.found and got.transient
    assert db.art(DKC) is None


async def test_server_error_records_no_miss(art: ArtCache, net: Net, db: Database) -> None:
    net.respond = lambda r: httpx.Response(503)
    await art.get(DKC)
    assert db.art(DKC) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"x" * (5 * 1024 * 1024 + 1), headers={"content-type": "image/png"}),
        httpx.Response(302, headers={"location": "https://example.com/a.png"}),
    ],
    ids=["html", "too-big", "redirect"],
)
async def test_unusable_response_is_a_miss(art: ArtCache, net: Net, db: Database, response: httpx.Response) -> None:
    net.respond = lambda r: response
    got = await art.get(DKC)
    assert got is not None and not got.found
    row = db.art(DKC)
    assert row is not None and row.status == "none" and row.file is None
    assert len(net.requests) == 1


async def test_oversized_stream_is_cut_off_at_the_cap(art: ArtCache, net: Net, db: Database) -> None:
    chunk = b"x" * (1024 * 1024)
    read: list[int] = []

    async def body():  # type: ignore[no-untyped-def]
        for i in range(100):  # no Content-Length: only the byte count can stop it
            read.append(i)
            yield chunk

    net.respond = lambda r: httpx.Response(200, content=body(), headers={"content-type": "image/png"})
    got = await art.get(DKC)
    assert got is not None and not got.found
    row = db.art(DKC)
    assert row is not None and row.status == "none"
    assert len(read) <= 7  # 5 MB cap, 1 MB chunks


async def test_declared_oversize_is_refused_without_reading(art: ArtCache, net: Net) -> None:
    read: list[int] = []

    async def body():  # type: ignore[no-untyped-def]
        read.append(1)
        yield b"x"

    net.respond = lambda r: httpx.Response(
        200, content=body(), headers={"content-type": "image/png", "content-length": str(6 * 1024 * 1024)}
    )
    got = await art.get(DKC)
    assert got is not None and not got.found
    assert read == []


async def test_requests_ask_for_identity_encoding(art: ArtCache, net: Net) -> None:
    await art.get(DKC)
    assert net.requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("failure", ["network", "5xx"])
async def test_transient_failure_pauses_libretro(
    art: ArtCache, net: Net, db: Database, clock: Clock, failure: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError("down")
        return httpx.Response(503)

    net.respond = respond
    await art.get(DKC)
    await art.get(FACADE)
    assert len(net.requests) == 1
    assert db.art(DKC) is None
    clock.advance(61)
    await art.get(FACADE)
    assert len(net.requests) == 2


async def test_queued_requests_do_not_fire_after_a_429(art: ArtCache, net: Net) -> None:
    import asyncio

    net.respond = lambda r: httpx.Response(429, headers={"retry-after": "120"})
    await asyncio.gather(art.get(DKC), art.get(FACADE), art.get(GUNSTAR))
    assert len(net.requests) == 1


async def test_acquire_timeout_is_unknown_not_a_miss(art: ArtCache, net: Net, db: Database) -> None:
    art._bucket.tokens = 0.0
    art._bucket.rate = 0.01  # a token is 100 s away, beyond the acquire timeout
    got = await art.get(DKC)
    assert got is not None and not got.found
    assert net.requests == [] and db.art(DKC) is None


async def test_storage_failure_serves_art_and_leaks_no_path(
    art: ArtCache, net: Net, db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    net.respond = png_response

    def fail(self: Path, data: bytes) -> int:
        raise OSError(13, f"Permission denied: '{self}'")

    monkeypatch.setattr(Path, "write_bytes", fail)
    with caplog.at_level(logging.DEBUG):
        got = await art.get(DKC)
    assert got is not None and got.found and got.body == tiny_png()
    assert db.art(DKC) is None
    assert str(art.art_dir) not in caplog.text
    assert "Could not cache art" in caplog.text


async def test_prefetch_error_log_has_no_exception_text(
    art: ArtCache, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(game_id: str) -> None:
        raise OSError(f"cannot write {art.art_dir}/x")

    monkeypatch.setattr(art, "get", boom)
    await art.prefetch([DKC])
    assert "OSError" in caplog.text and str(art.art_dir) not in caplog.text


async def test_offline_holder_means_unknown_not_a_miss(
    art: ArtCache, net: Net, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(art.registry, "status", lambda name: DeviceStatus("offline", "offline", None, None))
    got = await art.get(DKC)
    assert got is not None and not got.found
    assert len(net.requests) == 1  # libretro answered 404
    assert db.art(DKC) is None


async def test_content_type_sets_extension(art: ArtCache, net: Net, db: Database) -> None:
    net.respond = lambda r: httpx.Response(200, content=b"jpegdata", headers={"content-type": "image/jpeg"})
    await art.get(DKC)
    row = db.art(DKC)
    assert row is not None and row.file == f"{DKC[:2]}/{DKC}.jpg" and row.content_type == "image/jpeg"


async def test_missing_cache_file_is_refetched(art: ArtCache, net: Net, db: Database) -> None:
    net.respond = png_response
    await art.get(DKC)
    row = db.art(DKC)
    assert row is not None and row.file is not None
    (art.art_dir / row.file).unlink()
    got = await art.get(DKC)
    assert got is not None and got.found
    assert len(net.requests) == 2
    assert (art.art_dir / row.file).is_file()


async def test_online_off_skips_libretro(art: ArtCache, net: Net, db: Database) -> None:
    art._online = False
    got = await art.get(DKC)
    assert got is not None and not got.found
    assert net.requests == []


async def test_unknown_system_skips_libretro(art: ArtCache, net: Net) -> None:
    saved = PLAYLISTS.pop("SNES")
    try:
        got = await art.get(DKC)
    finally:
        PLAYLISTS["SNES"] = saved
    assert got is not None and not got.found
    assert net.requests == []


async def test_unknown_game_is_none(art: ArtCache) -> None:
    assert await art.get("zzzzzzzzzzzzzzzz") is None


async def test_single_flight(art: ArtCache, net: Net) -> None:
    import asyncio

    net.respond = png_response
    a, b = await asyncio.gather(art.get(DKC), art.get(DKC))
    assert a == b
    assert len(net.requests) == 1


async def test_prefetch_skips_cached_and_swallows_errors(art: ArtCache, net: Net, db: Database) -> None:
    net.respond = png_response
    await art.get(DKC)
    assert len(net.requests) == 1
    await art.prefetch([DKC, FACADE, "nonexistent00000"])
    assert len(net.requests) == 2
    assert db.art(FACADE) is not None


def test_placeholder_escapes_title() -> None:
    svg = placeholder_svg("<script>alert(1)</script>", "SNES").decode()
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert 'viewBox="0 0 300 400"' in svg


def test_placeholder_uses_system_color_and_default() -> None:
    assert SYSTEM_COLORS["SNES"] in placeholder_svg("x", "SNES").decode()
    assert DEFAULT_COLOR in placeholder_svg("x", "Unlisted").decode()


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_placeholder_text_contrast() -> None:
    colors = {**SYSTEM_COLORS, "default": DEFAULT_COLOR}
    for name, color in colors.items():
        assert re.fullmatch(r"#[0-9a-f]{6}", color), name
        assert 1.05 / (_luminance(color) + 0.05) >= 4.5, name


async def test_placeholder_is_served_for_long_titles(art: ArtCache) -> None:
    got = await art.get(GUNSTAR)
    assert got is not None and got.body.startswith(b"<svg")


async def test_no_real_network(art: ArtCache, net: Net) -> None:
    # Net.handler asserts the host; any other host would fail the request and so this run.
    net.respond = png_response
    for g in (DKC, FACADE, GUNSTAR):
        assert await art.get(g) is not None
    assert {r.url.host for r in net.requests} <= {"thumbnails.libretro.com"}
