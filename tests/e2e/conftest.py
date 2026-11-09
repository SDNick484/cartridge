"""Playwright harness: a real `cartridge simulate` process per run, driven through Chromium (sync API)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

START_TIMEOUT = 40.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Server:
    def __init__(self, directory: Path) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._log = (directory / "server.log").open("wb")
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cartridge.cli",
                "simulate",
                "--dir",
                str(directory),
                "--lan-bind",
                f"127.0.0.1:{self.port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=self._log,
        )
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"simulator exited early with code {self._proc.returncode}")
            try:
                with urllib.request.urlopen(f"{self.url}/healthz", timeout=1) as r:
                    if r.status == 200 and self._synced():
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError("simulator did not become ready")

    def _synced(self) -> bool:
        with urllib.request.urlopen(f"{self.url}/api/config", timeout=2) as r:
            return bool(json.load(r)["synced"])

    def stop(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._log.close()


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Read-only tests share this one; tests that launch use `fresh_server`."""
    s = Server(tmp_path_factory.mktemp("sim"))
    yield s.url
    s.stop()


@pytest.fixture
def fresh_server(tmp_path: Path) -> Iterator[str]:
    s = Server(tmp_path)
    yield s.url
    s.stop()


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def open_page(browser: Browser) -> Iterator[Callable[..., Page]]:
    """Factory: open_page(url, width=412, height=915, init_script=None) -> Page that records console problems."""
    contexts: list[BrowserContext] = []

    def make(url: str, width: int = 412, height: int = 915, init_script: str | None = None) -> Page:
        ctx = browser.new_context(viewport={"width": width, "height": height})
        contexts.append(ctx)
        if init_script:
            ctx.add_init_script(init_script)
        page = ctx.new_page()
        problems: list[str] = []
        page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.problems = problems  # type: ignore[attr-defined]
        page.goto(url)
        return page

    yield make
    for ctx in contexts:
        ctx.close()
