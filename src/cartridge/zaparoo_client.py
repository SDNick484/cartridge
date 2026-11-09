"""Zaparoo Core's API: one long-lived WebSocket per MiSTer, speaking JSON-RPC 2.0.

Local copy pending promotion to mcp-server-shared (spec §11); derived from mcp-server-misterfpga (MIT).

Why our own client and not a library: the API is small and documented as stable (``/api/v0.1``, additive
changes only), and every method Cartridge may send fits in one frozenset. A thin client keeps that
allow-list in one place and lets the tests run the real client against a fake Core on a real socket.

How it works:
- ``start()`` launches a background task that connects and reconnects with backoff (``BACKOFF_START``
  doubling to ``BACKOFF_MAX``; it resets only once a connection is usable, so a Core that accepts the socket
  and then refuses or closes is not hammered). The first attempt sets ``tried``, so callers can wait for it briefly.
- Requests carry a uuid4 ``id``; responses are matched back to their futures. Several requests can be
  in flight on one socket.
- Notifications (no ``id``) are passed to listeners. State listeners hear ``True`` once a connection is
  up and its version read, and ``False`` when it drops.
- Liveness: Zaparoo answers the text ``ping`` with ``pong`` (ASSUMPTION Z-ENDPOINT). We send one every
  ``PING_EVERY`` and drop the connection if nothing at all has arrived for ``SILENCE_LIMIT``, so a MiSTer
  that was switched off is noticed although no TCP error ever arrives.
- No authentication: on MiSTer, Zaparoo admits unauthenticated LAN clients as "legacy"
  (ASSUMPTION Z-LEGACY), which is enough for every method below.
- Encryption: a Core with encryption on answers every request, ``version`` included, with -32002
  (ASSUMPTION Z-ENC-ERROR). The client then sets ``needs_pairing`` and drops the socket rather than
  holding it open: an open socket that can't do anything is not "connected", and a fresh attempt with
  backoff notices as soon as encryption is turned off. ``needs_pairing`` clears when a later connection
  reads the version.
- ``connected`` means "usable": the socket is up and the version read has finished (a failure other than
  encryption still leaves it usable; the next call reports the real problem).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import socket
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

import aiohttp

from .tasks import stop_task

log = logging.getLogger(__name__)

API_PATH = "/api/v0.1"  # ASSUMPTION Z-ENDPOINT

# Everything Cartridge may ever send (spec §9.3). Nothing under settings.*, device.*, update.*, clients.*,
# input.*, readers.*, and no media.generate or `unsafe` run. tests/test_safety.py pins this set, so
# widening it is a deliberate, reviewed change.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "version",
        "media",
        "media.search",
        "media.meta",
        "media.image",
        "media.active",
        "media.history",
        "systems",
        "launchers",
        "run",
        "stop",
    }
)

DEFAULT_TIMEOUT = 10.0
RUN_TIMEOUT = 35.0  # Core waits up to 30 s for a launch itself (ASSUMPTION Z-RUN-WAITS)
PING_EVERY = 20.0
SILENCE_LIMIT = 60.0
CONNECT_TIMEOUT = 10.0
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
ENCRYPTION_REQUIRED = -32002  # ASSUMPTION Z-ENC-ERROR

Listener = Callable[[str, dict[str, Any]], None]
StateListener = Callable[[bool], None]


class ZaparooUnavailable(Exception):
    """No usable connection to Zaparoo right now (the message says why)."""


class ZaparooTimeout(Exception):
    """Zaparoo didn't answer in time; whatever it already started may still be going."""


class ZaparooError(Exception):
    """Zaparoo answered with a JSON-RPC error."""

    def __init__(
        self, message: str, *, code: int | None = None, category: str | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.reason = reason


class EncryptionRequired(ZaparooError):
    """Core has encryption on and we haven't paired with it."""


def describe_failure(exc: BaseException, label: str, port: int, setup_hint: str) -> str:
    """One sentence saying why we couldn't reach Zaparoo, and what to check."""
    os_error = exc.os_error if isinstance(exc, aiohttp.ClientConnectorError) else exc
    if isinstance(os_error, OSError) and os_error.errno == errno.ECONNREFUSED:
        return (
            f"Zaparoo Core isn't running on {label} (connection refused on port {port}). "
            f"The MiSTer is on, but nothing is listening. {setup_hint}"
        )
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        return f"{label} answered on port {port}, but not as Zaparoo's API ({exc.status}): is this Zaparoo Core?"
    if (
        isinstance(exc, TimeoutError | aiohttp.ServerTimeoutError | socket.gaierror)
        or isinstance(os_error, socket.gaierror)
        or (
            isinstance(os_error, OSError) and os_error.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT)
        )
    ):
        return f"No answer from {label}: is it on and on the network?"
    # No host and no str(exc) here: aiohttp embeds host:port in it, and this sentence reaches the UI.
    return f"Can't reach Zaparoo on {label} ({exc.__class__.__name__}); the server log has details."


class ZaparooClient:
    def __init__(
        self,
        label: str,
        host: str,
        port: int,
        *,
        allowed_methods: frozenset[str] = ALLOWED_METHODS,
        setup_hint: str,
        transport: Literal["legacy"] = "legacy",
    ) -> None:
        self.label = label
        self.host = host
        self.port = port
        self.allowed_methods = allowed_methods
        self.setup_hint = setup_hint
        self.transport = transport
        self.url = f"ws://{host}:{port}{API_PATH}"
        self.connected = False
        self.version: str | None = None
        self.error: str | None = None
        self.needs_pairing = False
        self.tried = asyncio.Event()  # set once the first connection attempt has finished, either way
        self._listeners: list[Listener] = []
        self._state_listeners: list[StateListener] = []
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_heard = 0.0
        self._pairing_announced = False
        self._backoff = BACKOFF_START

    # --- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"zaparoo {self.label}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        await stop_task(task, 3.0, f"Zaparoo connection to {self.label}")
        self._fail_pending(ZaparooUnavailable(f"Zaparoo connection to {self.label} closed"))

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def add_state_listener(self, listener: StateListener) -> None:
        """Called with True when a connection is ready (version read), False when it's lost."""
        self._state_listeners.append(listener)

    def _state(self, up: bool) -> None:
        for listener in list(self._state_listeners):
            try:
                listener(up)
            except Exception:  # a listener's bug must not kill the connection
                log.exception("%s: state listener failed", self.label)

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    # ASSUMPTION Z-LEGACY: no credentials; Core treats an unauthenticated LAN client as legacy.
                    async with session.ws_connect(self.url, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                        await self._serve(ws)
                    if self.error is None:
                        self.error = f"Zaparoo on {self.label} closed the connection; reconnecting"
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    self.error = describe_failure(exc, self.label, self.port, self.setup_hint)
                    log.info("%s: %s", self.label, self.error)
                    log.debug("%s: connect failed: %r", self.label, exc)
                finally:
                    was_up, self.connected = self.connected, False
                    self._ws = None
                    if was_up:
                        self._state(False)
                    self._fail_pending(ZaparooUnavailable(self.error or f"Zaparoo on {self.label} disconnected"))
                self.tried.set()
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._last_heard = time.monotonic()
        log.info("%s: socket open to Zaparoo Core", self.label)
        pinger = asyncio.create_task(self._ping(ws), name=f"zaparoo ping {self.label}")
        primer = asyncio.create_task(self._prime(ws), name=f"zaparoo prime {self.label}")
        try:
            async for msg in ws:
                self._last_heard = time.monotonic()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            await stop_task(pinger, 1.0, f"Zaparoo ping for {self.label}")
            await stop_task(primer, 1.0, f"Zaparoo version read for {self.label}")

    async def _prime(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read the version once per connection; the connection counts as usable once that's done."""
        try:
            if "version" in self.allowed_methods:
                info = await self._request("version")
                self.version = str(info.get("version")) if isinstance(info, dict) else None
        except EncryptionRequired:
            self.needs_pairing = True
            self.error = "needs pairing (Zaparoo encryption is on)"
            log.warning("%s: %s", self.label, self.error)
            self.tried.set()
            if not self._pairing_announced:
                self._pairing_announced = True
                self._state(False)
            await ws.close()  # reconnect with backoff rather than hold a socket that can't do anything
            return
        except (ZaparooError, ZaparooTimeout, ZaparooUnavailable) as exc:
            log.warning("%s: couldn't read Zaparoo's version: %s", self.label, exc)
        self.needs_pairing = False
        self._pairing_announced = False
        self.error = None
        self._backoff = BACKOFF_START  # only a usable connection resets the backoff
        self.connected = True
        self.tried.set()
        self._state(True)

    async def _ping(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(PING_EVERY)
            if time.monotonic() - self._last_heard > SILENCE_LIMIT:
                self.error = f"Zaparoo on {self.label} stopped answering; reconnecting"
                log.warning("%s", self.error)
                await ws.close()
                return
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.send_str("ping")  # ASSUMPTION Z-ENDPOINT: answered with "pong"

    # --- messages ----------------------------------------------------------------------------
    def _handle(self, text: str) -> None:
        if text == "pong":
            return
        try:
            msg = json.loads(text)
        except ValueError:
            log.debug("%s: ignoring a frame that isn't JSON", self.label)
            return
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if isinstance(msg_id, str) and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if fut.done():
                return
            if msg.get("error") is not None:
                fut.set_exception(self._error(msg["error"]))
            else:
                fut.set_result(msg.get("result"))
        elif msg_id is None and isinstance(msg.get("method"), str):
            params = msg.get("params")
            self._notify(msg["method"], params if isinstance(params, dict) else {})
        elif msg_id is None and "error" in msg:
            log.warning("%s: Zaparoo protocol error: %s", self.label, msg["error"])

    def _error(self, err: Any) -> ZaparooError:
        if not isinstance(err, dict):
            return ZaparooError(f"Zaparoo on {self.label} returned an error: {err!r}")
        raw = err.get("data")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        category = data.get("category") if isinstance(data.get("category"), str) else None
        reason = data.get("reason") if isinstance(data.get("reason"), str) else None
        code = err.get("code") if isinstance(err.get("code"), int) else None
        cls = EncryptionRequired if code == ENCRYPTION_REQUIRED else ZaparooError
        return cls(str(err.get("message") or "error"), code=code, category=category, reason=reason)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(method, params)
            except Exception:  # a listener's bug must not kill the connection
                log.exception("%s: notification listener failed on %s", self.label, method)

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Send one request and return its ``result``. Raises ZaparooError for an error reply."""
        self._check(method, params)
        if not self.connected:
            raise ZaparooUnavailable(self.error or f"Zaparoo on {self.label} isn't connected yet")
        return await self._request(method, params, timeout)

    def _check(self, method: str, params: dict[str, Any] | None) -> None:
        # Run by every request, the version prime included, so nothing reaches the socket without it.
        if method not in self.allowed_methods:
            raise ValueError(f"{method!r} is not a Zaparoo method Cartridge sends")  # a bug, not user input
        if params is not None and "unsafe" in params:
            raise ValueError("the 'unsafe' parameter is never sent to Zaparoo")

    async def _request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> Any:
        self._check(method, params)
        ws = self._ws
        if ws is None:
            raise ZaparooUnavailable(self.error or f"Zaparoo on {self.label} isn't connected yet")
        req_id = str(uuid.uuid4())
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            frame["params"] = params
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await ws.send_str(json.dumps(frame))
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError:
            raise ZaparooTimeout(
                f"Zaparoo on {self.label} didn't answer {method} within {timeout:g}s. "
                "Anything it already started keeps going."
            ) from None
        except (ConnectionError, RuntimeError) as exc:
            raise ZaparooUnavailable(f"Lost the connection to Zaparoo on {self.label}: {exc}") from None
        finally:
            self._pending.pop(req_id, None)
