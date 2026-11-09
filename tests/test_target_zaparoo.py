import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from cartridge import zaparoo_client as zc
from cartridge.config import DeviceConfig
from cartridge.ids import normalize_rel_path
from cartridge.sim.fake_zaparoo import RpcError
from cartridge.sim.harness import SimDevice, Simulation
from cartridge.targets import zaparoo as zt
from cartridge.targets.base import (
    Blocked,
    CatalogEntry,
    DeviceBusy,
    Gone,
    LaunchError,
    LaunchFailed,
    LaunchTimeout,
    Offline,
    StaleRef,
    TargetError,
    TargetEvent,
)
from cartridge.targets.zaparoo import ZaparooTarget

from .conftest import family, office

pytestmark = pytest.mark.anyio

Make = Callable[..., ZaparooTarget]


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zc, "BACKOFF_START", 0.05)
    monkeypatch.setattr(zc, "BACKOFF_MAX", 0.1)


def config_for(dev: SimDevice, **extra: Any) -> DeviceConfig:
    return DeviceConfig(
        name=dev.name,
        label=dev.fake.name,
        model=dev.fake.model,
        platform="mister",
        transport="zaparoo",
        host="127.0.0.1",
        port=dev.fake.port,
        **extra,
    )


@pytest.fixture
async def make() -> AsyncIterator[Make]:
    targets: list[ZaparooTarget] = []

    def build(dev: SimDevice, **extra: Any) -> ZaparooTarget:
        t = ZaparooTarget(config_for(dev, **extra))
        targets.append(t)
        return t

    yield build
    for t in targets:
        await t.close()


async def until(pred: Callable[[], bool], limit: float = 3.0) -> None:
    async with asyncio.timeout(limit):
        while not pred():
            await asyncio.sleep(0.01)


async def online(t: ZaparooTarget) -> ZaparooTarget:
    await t.start()
    await until(lambda: t.status().online)
    return t


async def collect(t: ZaparooTarget) -> list[CatalogEntry]:
    return [e async for e in t.catalog()]


async def test_catalog_pages_and_yields_every_item(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    fake.page_cap = 2
    t = await online(make(family(sim)))
    entries = await collect(t)
    assert len(entries) == len(fake.media)
    by_id = {int(e.ref.token): e for e in entries}
    for m in fake.media:
        e = by_id[m["mediaId"]]
        assert e.rel_path == normalize_rel_path(m["relativePath"])
        assert e.abs_path == m["path"]
        assert e.title == m["name"]
        assert e.system_id == m["system"]["id"]
    searches = [p for meth, p in fake.received if meth == "media.search"]
    assert len(searches) > len(fake.systems) and all(p["maxResults"] == zt.SEARCH_PAGE for p in searches)


async def test_catalog_falls_back_to_meta_batch(sim: Simulation, make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = family(sim).fake
    monkeypatch.setattr(zt, "META_BATCH", 4)
    fake.search_relative_path = False
    t = await online(make(family(sim)))
    entries = await collect(t)
    assert {e.rel_path for e in entries} == {normalize_rel_path(m["relativePath"]) for m in fake.media}
    batches = [p["items"] for meth, p in fake.received if meth == "media.meta"]
    assert batches and all(0 < len(b) <= 4 for b in batches)
    assert sum(len(b) for b in batches) == len(fake.media)


async def test_meta_batch_never_exceeds_100(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    fake.search_relative_path = False
    base = fake.media[0]
    fake.media.extend(
        {**base, "mediaId": 5000 + i, "relativePath": f"x{i}.sfc", "path": f"/media/fat/games/SNES/x{i}.sfc"}
        for i in range(150)
    )
    t = await online(make(family(sim)))
    entries = await collect(t)
    assert len(entries) == len(fake.media)
    assert max(len(p["items"]) for meth, p in fake.received if meth == "media.meta") <= zt.META_BATCH


async def test_strip_prefixes_overrides_relative_path(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    for m in fake.media:
        m["relativePath"] = "ignored/" + m["relativePath"]
    t = await online(make(family(sim), strip_prefixes=("/media/fat/games",)))
    entries = await collect(t)
    assert {e.rel_path for e in entries} == {normalize_rel_path(m["path"].split("/", 5)[5]) for m in fake.media}
    assert not any(e.rel_path.startswith("ignored/") for e in entries)


async def test_catalog_skips_unresolvable_rows(sim: Simulation, make: Make, caplog: pytest.LogCaptureFixture) -> None:
    fake = family(sim).fake
    fake.media[0]["relativePath"] = ""
    fake.search_relative_path = False
    t = await online(make(family(sim)))
    entries = await collect(t)
    assert len(entries) == len(fake.media) - 1
    assert "no usable relative path" in caplog.text


async def test_catalog_with_nothing_usable_raises_one_sentence(
    sim: Simulation, make: Make, caplog: pytest.LogCaptureFixture
) -> None:
    fake = family(sim).fake
    fake.search_relative_path = False
    for m in fake.media:
        m["relativePath"] = ""
    t = await online(make(family(sim)))
    with pytest.raises(TargetError) as info:
        await collect(t)
    assert str(info.value) == (
        f"Family Room reported {len(fake.media)} games but none had a usable path; "
        "check strip_prefixes in devices.yaml (see README)."
    )
    assert caplog.text.count("no usable relative path") <= 1


async def test_catalog_skips_log_one_summary(sim: Simulation, make: Make, caplog: pytest.LogCaptureFixture) -> None:
    fake = family(sim).fake
    fake.search_relative_path = False
    fake.media[0]["relativePath"] = ""
    fake.media[1]["relativePath"] = ""
    t = await online(make(family(sim)))
    assert len(await collect(t)) == len(fake.media) - 2
    assert caplog.text.count("no usable relative path") == 1 and "skipped 2" in caplog.text


async def test_title_fallback_is_the_file_stem(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    fake.media[0]["name"] = ""
    fake.media[0]["relativePath"] = "Sub/Dir/Some Game.sfc"
    t = await online(make(family(sim)))
    entry = next(e for e in await collect(t) if e.ref.token == str(fake.media[0]["mediaId"]))
    assert entry.title == "Some Game"


async def test_catalog_error_midway_propagates(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    fake.page_cap = 1
    t = await online(make(family(sim)))
    seen = 0
    with pytest.raises(TargetError, match="Family Room"):
        async for _ in t.catalog():
            seen += 1
            if seen == 2:
                fake.close_after = "media.search"
    assert 2 <= seen < len(fake.media)


async def test_systems_availability_from_launchers(sim: Simulation, make: Make) -> None:
    t = await online(make(office(sim)))
    systems = {s.system_id: s for s in await t.systems()}
    assert systems["Genesis"].available is False
    assert systems["Genesis"].media_count > 0
    assert systems["SNES"].available is True
    assert systems["SNES"].manufacturer == "Nintendo"
    assert systems["SNES"].release_date == "1990-11-21"


async def test_status_states(sim: Simulation, make: Make) -> None:
    t = make(family(sim))
    assert t.status().state == "connecting"
    await t.start()
    await until(lambda: t.status().online)
    st = t.status()
    assert st.message is None and st.version == "2.20.0" and st.now_playing is None

    off = office(sim).fake
    off.refuse = True
    t2 = make(office(sim))
    await t2.start()
    await until(lambda: t2.status().state == "offline")
    msg = t2.status().message
    assert msg and "Office" in msg and "127.0.0.1" not in msg

    fam = family(sim)
    await t.close()
    fam.fake.require_encryption = True
    t3 = make(fam)
    await t3.start()
    await until(lambda: t3.status().state == "needs_pairing")
    msg3 = t3.status().message
    assert msg3 and "Family Room" in msg3 and "127.0.0.1" not in msg3


async def test_now_playing_tracks_notifications(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    events: list[TargetEvent] = []
    t.subscribe(events.append)
    media = fake.media[0]
    await fake.play(media["mediaId"])
    await until(lambda: t.status().now_playing is not None)
    playing = t.status().now_playing
    assert playing is not None and playing.media_name == media["name"] and playing.abs_path == media["path"]
    await fake.quit_game()
    await until(lambda: t.status().now_playing is None)
    assert [e.kind for e in events] == ["media.started", "media.stopped"]
    assert all(e.device == family(sim).name for e in events)


async def test_connect_reads_active_game(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    await fake.play(fake.media[1]["mediaId"])
    t = await online(make(family(sim)))
    await until(lambda: t.status().now_playing is not None)
    now = t.status().now_playing
    assert now is not None and now.media_name == fake.media[1]["name"]


async def test_raising_listener_does_not_break_target(
    sim: Simulation, make: Make, caplog: pytest.LogCaptureFixture
) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    good: list[TargetEvent] = []

    def bad(_: TargetEvent) -> None:
        raise RuntimeError("boom")

    t.subscribe(bad)
    t.subscribe(good.append)
    await fake.play(fake.media[0]["mediaId"])
    await until(lambda: bool(good))
    assert t.status().now_playing is not None
    assert "event listener failed" in caplog.text


@pytest.mark.parametrize("after_reply", [False, True])
async def test_launch_success(sim: Simulation, make: Make, after_reply: bool) -> None:
    fake = family(sim).fake
    fake.started_after_reply = after_reply
    t = await online(make(family(sim)))
    entry = next(e for e in await collect(t) if e.title == "Super Metroid")
    await t.launch(entry.ref)
    await until(lambda: t.status().now_playing is not None)
    now = t.status().now_playing
    assert now is not None and now.media_name == "Super Metroid"
    assert fake.methods[-2:] == ["media.meta", "run"]


async def test_launch_stale_ref_after_reindex(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    fake.reindex()
    with pytest.raises(StaleRef):
        await t.launch(ref)
    assert "run" not in fake.methods


async def test_launch_stale_when_path_differs(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    fake.media[0]["relativePath"] = "Renamed.sfc"
    with pytest.raises(StaleRef):
        await t.launch(ref)
    assert "run" not in fake.methods


async def test_launch_missing_file(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    fake.media[0]["isMissing"] = True
    with pytest.raises(Gone, match="sync again") as info:
        await t.launch(ref)
    assert "Family Room" in str(info.value)
    assert "run" not in fake.methods


@pytest.mark.parametrize(
    ("category", "cls"),
    [
        ("media_not_found", Gone),
        ("busy", DeviceBusy),
        ("blocked", Blocked),
        ("launch_repair", LaunchFailed),
        ("timeout", LaunchTimeout),
    ],
)
async def test_run_error_categories(sim: Simulation, make: Make, category: str, cls: type[LaunchError]) -> None:
    fake = family(sim).fake
    fake.run_error = category
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    with pytest.raises(cls) as info:
        await t.launch(ref)
    assert type(info.value) is cls
    assert "Family Room" in str(info.value) and "127.0.0.1" not in str(info.value)
    if cls is LaunchFailed:
        assert isinstance(info.value, LaunchFailed) and info.value.reason == "launcher_plugin_missing"


async def test_launch_repair_message_never_carries_the_raw_reason(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    fake.run_error = "launch_repair"
    fake.run_reason = "cannot open /media/fat/games/SNES/a.sfc"
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    with pytest.raises(LaunchFailed) as info:
        await t.launch(ref)
    assert "/media" not in str(info.value) and "a.sfc" not in str(info.value)
    assert "Family Room" in str(info.value) and "core" in str(info.value)


async def test_launch_replace_stops_first(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    entries = await collect(t)
    await fake.play(fake.media[0]["mediaId"])
    await until(lambda: t.status().now_playing is not None)
    await t.launch(entries[1].ref, replace=True)
    methods = fake.methods
    assert methods[methods.index("stop") - 1] == "media.meta"
    assert methods[-1] == "run"
    await until(lambda: getattr(t.status().now_playing, "media_name", None) == entries[1].title)


async def test_launch_without_replace_does_not_stop(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    await t.launch(ref)
    assert "stop" not in fake.methods


async def test_return_to_menu(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    await fake.play(fake.media[0]["mediaId"])
    await until(lambda: t.status().now_playing is not None)
    await t.return_to_menu()
    assert fake.active is None
    await until(lambda: t.status().now_playing is None)


async def test_return_to_menu_times_out(sim: Simulation, make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = family(sim).fake
    monkeypatch.setattr(zt, "STOP_WAIT", 0.2)
    t = await online(make(family(sim)))
    await fake.play(fake.media[0]["mediaId"])
    await until(lambda: t.status().now_playing is not None)

    async def stays_running() -> None:  # Core accepts stop but the game keeps running
        return None

    monkeypatch.setattr(fake, "quit_game", stays_running)
    with pytest.raises(LaunchTimeout) as info:
        await t.return_to_menu()
    assert "Family Room" in str(info.value)


async def test_indexing_finished_event(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    events: list[TargetEvent] = []
    t.subscribe(events.append)
    await fake.notify("media.indexing", {"exists": True, "indexing": True})
    await fake.finish_indexing()
    await until(lambda: bool(events))
    assert [e.kind for e in events] == ["indexing.finished"]


async def test_drop_during_run_is_offline(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    fake.close_after = "run"
    with pytest.raises(Offline) as info:
        await t.launch(ref)
    assert "Family Room" in str(info.value) and "127.0.0.1" not in str(info.value)


async def test_artwork_and_history(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    entries = await collect(t)
    with_art = next(e for e in entries if int(e.ref.token) == fake.media[0]["mediaId"])
    img = await t.artwork(with_art.ref, 512)
    assert img is not None and img.content_type == "image/png" and img.data.startswith(b"\x89PNG")
    without = next(e for e in entries if not fake.by_id(int(e.ref.token)).get("image"))  # type: ignore[union-attr]
    assert await t.artwork(without.ref, 512) is None
    hist = await t.history(10)
    assert hist and hist[0].media_name == fake.history[0]["mediaName"]
    assert hist[0].play_time == fake.history[0]["playTime"]
    media_history = [p for meth, p in fake.received if meth == "media.history"]
    assert media_history == [{"limit": 10, "distinctMedia": True}]


async def test_offline_target_errors_are_sentences(sim: Simulation, make: Make) -> None:
    t = make(family(sim))
    ref = zt.TargetRef("1", "x")
    with pytest.raises(Offline):
        await t.launch(ref)
    with pytest.raises(TargetError, match="Family Room"):
        await t.artwork(ref, 512)  # unreachable is not "no art"


async def test_artwork_transport_failures_raise(sim: Simulation, make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    fake.close_after = "media.image"
    with pytest.raises(TargetError):
        await t.artwork(ref, 512)
    t = await online(make(family(sim)))
    fake.close_after = None
    fake.silent = True
    call = t._client.call
    monkeypatch.setattr(t._client, "call", lambda m, p=None: call(m, p, timeout=0.1))
    with pytest.raises(TargetError, match="didn't answer"):
        await t.artwork(ref, 512)


async def test_strip_prefixes_skips_rows_outside_prefix(
    sim: Simulation, make: Make, caplog: pytest.LogCaptureFixture
) -> None:
    fake = family(sim).fake
    outside = fake.media[0]
    outside["path"] = "/elsewhere/SNES/" + outside["relativePath"]
    fake.search_relative_path = False
    t = await online(make(family(sim), strip_prefixes=("/media/fat/games",)))
    entries = await collect(t)
    assert str(outside["mediaId"]) not in {e.ref.token for e in entries}
    assert len(entries) == len(fake.media) - 1
    assert "Family Room" in caplog.text and "skipped 1 games" in caplog.text
    assert "media.meta" not in fake.methods  # rel_path comes from the absolute path, so no lookup


async def test_reads_raise_target_error_sentences(sim: Simulation, make: Make) -> None:
    t = make(family(sim))  # never started: not connected
    for call in (t.systems(), t.history(5)):
        with pytest.raises(TargetError) as info:
            await call
        assert "Family Room" in str(info.value) and "127.0.0.1" not in str(info.value)
    with pytest.raises(TargetError):
        await collect(t)


async def test_read_error_reply_is_target_error(sim: Simulation, make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = family(sim).fake
    original = fake._call

    async def failing(method: str, p: dict[str, Any], after: list[tuple[str, dict[str, Any]]]) -> Any:
        if method == "media.history":
            raise RpcError("history unavailable")
        return await original(method, p, after)

    monkeypatch.setattr(fake, "_call", failing)
    t = await online(make(family(sim)))
    with pytest.raises(TargetError, match="Family Room"):
        await t.history(5)


async def test_launch_encryption_after_connect_is_offline_not_stale(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    fake.require_encryption = True
    with pytest.raises(Offline) as info:
        await t.launch(ref)
    assert "pairing" in str(info.value) and "Family Room" in str(info.value)


async def test_launch_without_zap_script_fails_with_sentence(sim: Simulation, make: Make) -> None:
    fake = family(sim).fake
    t = await online(make(family(sim)))
    ref = (await collect(t))[0].ref
    fake.media[0]["zapScript"] = ""
    with pytest.raises(LaunchFailed) as info:
        await t.launch(ref)
    assert "Family Room" in str(info.value)
    assert "run" not in fake.methods


async def test_needs_pairing_state_gives_pairing_sentence_on_reads_and_launch(sim: Simulation, make: Make) -> None:
    dev = family(sim)
    dev.fake.require_encryption = True
    t = make(dev)
    await t.start()
    await until(lambda: t.status().state == "needs_pairing")
    with pytest.raises(TargetError) as read_err:
        await t.systems()
    assert "Family Room" in str(read_err.value) and "pairing" in str(read_err.value)
    with pytest.raises(Offline) as launch_err:
        await t.launch(zt.TargetRef("1", "x"))
    assert "Family Room" in str(launch_err.value) and "pairing" in str(launch_err.value)
    with pytest.raises(Offline, match="pairing"):
        await t.return_to_menu()
