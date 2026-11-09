"""Browser smoke tests against `cartridge simulate` (spec 10, 1). Run with `pytest -m e2e`."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Locator, Page, expect

pytestmark = pytest.mark.e2e

OpenPage = Callable[..., Page]


def no_console_problems(page: Page, allowed: tuple[str, ...] = ()) -> None:
    """Includes CSP violations, which Chromium reports as console errors."""
    problems = [p for p in page.problems if p not in allowed]  # type: ignore[attr-defined]
    assert problems == []


def tiles(page: Page) -> Locator:
    return page.locator(".tile")


def pick_device(page: Page, label: str) -> None:
    page.locator("#device-pill").click()
    page.locator("#device-sheet").get_by_role("button", name=f"Use {label}").click()
    expect(page.locator("#device-pill")).to_contain_text(label)


def open_game(page: Page, title: str) -> None:
    page.locator(".tile", has_text=title).first.click()
    expect(page.locator("#game-sheet")).to_be_visible()


def test_phone_grid(server: str, open_page: OpenPage) -> None:
    page = open_page(server)
    expect(tiles(page).first).to_be_visible()
    ys = [round(t.bounding_box()["y"]) for t in tiles(page).all()[:6]]  # type: ignore[index]
    assert ys.count(ys[0]) >= 3
    assert page.evaluate("document.documentElement.scrollWidth") <= 412
    no_console_problems(page)


def test_pick_device_then_launch(fresh_server: str, open_page: OpenPage) -> None:
    page = open_page(fresh_server)
    open_game(page, "Super Metroid")
    play = page.locator("#play")
    expect(play).to_be_disabled()
    expect(page.locator("#game-sheet")).to_contain_text("Choose a device")
    page.keyboard.press("Escape")
    pick_device(page, "Family Room")
    open_game(page, "Super Metroid")
    expect(play).to_be_enabled()
    play.click()
    expect(page.locator("#toast")).to_contain_text("Super Metroid")
    page.locator("#device-pill").click()
    expect(page.locator("#device-sheet")).to_contain_text("Now playing: Super Metroid")
    no_console_problems(page)


def test_replace_confirm(fresh_server: str, open_page: OpenPage) -> None:
    page = open_page(fresh_server)
    pick_device(page, "Family Room")
    open_game(page, "Super Metroid")
    page.locator("#play").click()
    expect(page.locator("#toast")).to_contain_text("Super Metroid")
    open_game(page, "Gunstar Heroes")
    page.locator("#play").click()
    confirm = page.locator("#confirm-sheet")
    expect(confirm).to_contain_text("is running on Family Room. Replace it? Unsaved progress will be lost.")
    confirm.get_by_role("button", name="Replace").click()
    expect(page.locator("#toast")).to_contain_text("Gunstar Heroes")
    page.locator("#device-pill").click()
    expect(page.locator("#device-sheet")).to_contain_text("Now playing: Gunstar Heroes")
    # The 409 that asks for confirmation is the one expected failed request.
    no_console_problems(
        page, allowed=("Failed to load resource: the server responded with a status of 409 (Conflict)",)
    )


def test_filter_and_search(server: str, open_page: OpenPage) -> None:
    page = open_page(server)
    expect(tiles(page).first).to_be_visible()
    all_count = tiles(page).count()
    page.locator('.chip[data-key="system:SNES"]').click()
    page.wait_for_function(f"document.querySelectorAll('.tile').length < {all_count}")
    systems = {t.locator(".tile-sub").inner_text() for t in tiles(page).all()}
    assert systems == {"Super Nintendo"}
    page.locator('.chip[data-key="all"]').click()
    page.locator("#search").fill("facade")
    expect(tiles(page)).to_have_count(1)
    expect(tiles(page).first).to_contain_text("Façade & Friends: Part II")
    no_console_problems(page)


def test_unavailable_reason(server: str, open_page: OpenPage) -> None:
    page = open_page(server)
    pick_device(page, "Office")
    toggle = page.get_by_label("Only games for Office")
    expect(toggle).to_be_checked()
    toggle.uncheck()
    gunstar = page.locator(".tile", has_text="Gunstar Heroes")
    expect(gunstar).to_contain_text("core not installed")
    expect(gunstar).to_have_class(re.compile("unavailable"))
    no_console_problems(page)


def test_tv_keyboard_only(server: str, open_page: OpenPage) -> None:
    page = open_page(server + "/?tv=1", 1920, 1080)
    expect(page.locator("body")).to_have_class(re.compile(r"\btv\b"))
    expect(tiles(page).first).to_be_visible()
    for _ in range(30):  # reach the first tile with Tab alone
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.classList.contains('tile')"):
            break
    first = page.evaluate("document.activeElement.dataset.id")
    assert first
    page.keyboard.press("ArrowRight")
    second = page.evaluate("document.activeElement.dataset.id")
    assert second and second != first
    page.keyboard.press("ArrowDown")
    third = page.evaluate("document.activeElement.dataset.id")
    assert third and third != second
    page.keyboard.press("Enter")
    expect(page.locator("#game-sheet")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#game-sheet")).not_to_be_visible()
    assert page.evaluate("document.activeElement.dataset.id") == third
    assert page.evaluate("document.documentElement.scrollWidth") <= 1920
    no_console_problems(page)


def test_storage_blocked(server: str, open_page: OpenPage) -> None:
    blocked = """
      Object.defineProperty(window, 'localStorage', {
        get() { throw new DOMException('blocked', 'SecurityError'); },
      });
    """
    page = open_page(server, init_script=blocked)
    expect(tiles(page).first).to_be_visible()
    pick_device(page, "Family Room")
    no_console_problems(page)


WATCH_SYNC = """
  window.__finished = [];
  window.__ready = false;
  const es = new EventSource('/api/events');
  es.addEventListener('hello', () => { window.__ready = true; });
  es.addEventListener('sync.finished', (e) => window.__finished.push(e.data));
"""


def watch_sync(page: Page) -> None:
    """Records sync.finished frames; waits for the stream so a fast sync can't finish before it listens."""
    page.evaluate(WATCH_SYNC)
    page.wait_for_function("() => window.__ready")


def request_sync(page: Page, base: str, device: str) -> None:
    r = page.request.post(
        f"{base}/api/devices/{device}/sync",
        headers={"Content-Type": "application/json", "X-Cartridge": "1"},
        data="{}",
    )
    assert r.status == 202


def test_sync_finished_keeps_tv_focus(fresh_server: str, open_page: OpenPage) -> None:
    page = open_page(fresh_server + "/?tv=1", 1920, 1080)
    expect(tiles(page).first).to_be_visible()
    for _ in range(30):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.classList.contains('tile')"):
            break
    page.keyboard.press("ArrowRight")
    focused = page.evaluate("document.activeElement.dataset.id")
    assert focused
    watch_sync(page)
    request_sync(page, fresh_server, "family-room")
    page.wait_for_function("() => window.__finished.length > 0")
    assert '"added":0' in page.evaluate("window.__finished[0]")
    page.wait_for_timeout(500)  # let the app's own handler run
    assert page.evaluate("document.activeElement.dataset.id") == focused
    no_console_problems(page)


def test_sync_now_keeps_focus_in_device_sheet(fresh_server: str, open_page: OpenPage) -> None:
    page = open_page(fresh_server)
    expect(tiles(page).first).to_be_visible()
    watch_sync(page)
    page.locator("#device-pill").click()
    sync = page.locator('.device-card[data-device="family-room"] [data-role="sync"]')
    sync.click()
    page.wait_for_function("() => window.__finished.length > 0")
    page.wait_for_timeout(500)
    assert page.evaluate("document.activeElement.closest('#device-sheet') !== null")
    assert page.evaluate("document.activeElement.tagName") == "BUTTON"
    expect(sync).to_be_focused()
    no_console_problems(page)
