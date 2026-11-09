"""A fake Zaparoo Core: the real JSON-RPC-over-WebSocket protocol on a real socket.

Derived from mcp-server-misterfpga's sim/fake_zaparoo.py and sim/device.py (MIT, same author), minus
the SSH and NFC parts and with the device state folded in. Cartridge's own ZaparooClient connects to
it exactly as it would to a MiSTer, so tests exercise framing, id matching, notifications, errors and
reconnects, not a mock of our own code. The library (``media``, ``systems``, ``launchers``) and the
session state (``history``, ``active``) are plain attributes that tests may edit. Every received
method is recorded so tests can assert what the server did *not* send.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import struct
import zlib
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from aiohttp import WSMsgType, web

FIXTURE_MEDIA_ROOT = "/media/fat/games"
MAX_HISTORY = 100
MAX_META_BATCH = 100


def load_fixture(name: str) -> dict[str, Any]:
    text = resources.files("cartridge.sim").joinpath("fixtures", f"{name}.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def tiny_png(width: int = 4, height: int = 3) -> bytes:
    """A valid PNG (solid colour), standing in for cover art."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    raw = b"".join(b"\x00" + b"\x20\x40\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RpcError(Exception):
    def __init__(self, message: str, code: int = 1, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _launch_repair() -> RpcError:
    return RpcError(
        "this launcher's plugin for this system is not installed",
        data={"category": "launch_repair", "reason": "launcher_plugin_missing"},
    )


class FakeZaparoo:
    def __init__(self, fixture: dict[str, Any], host: str = "127.0.0.1", port: int = 0) -> None:
        data = copy.deepcopy(fixture)
        self.name: str = data["name"]
        self.model: str = data.get("model", "MiSTer FPGA")
        self.media: list[dict[str, Any]] = data["media"]
        self.systems: list[dict[str, Any]] = data["systems"]
        self.launchers: list[dict[str, Any]] = data["launchers"]
        self.history: list[dict[str, Any]] = data.get("history", [])  # newest first
        self.active: dict[str, Any] | None = None
        self.host = host
        self.port = port
        self.received: list[tuple[str, Any]] = []  # (method, params), in order
        # Failure switches (see the attribute docs in the task brief; refuse is a property below).
        self.require_encryption = False  # every request fails with -32002 (ASSUMPTION Z-ENC-ERROR)
        self.run_delay = 0.0
        self.run_error: str | None = None  # a data.category; "launch_repair" adds the reason
        self.silent = False  # accept frames but never answer (a hung Core)
        self.page_cap: int | None = None  # caps media.search maxResults
        self.search_relative_path = True  # False: search results omit relativePath (media.meta still has it)
        self.close_after: str | None = None  # close the socket on receiving this method, unanswered
        self._refuse = False
        self._listening = False
        self._transports: dict[web.WebSocketResponse, Any] = {}
        self._tasks: set[asyncio.Task[None]] = set()  # in-flight answers (kept so they aren't garbage collected)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.received]

    @property
    def _sockets(self) -> list[web.WebSocketResponse]:
        return list(self._transports)

    @property
    def refuse(self) -> bool:
        return self._refuse

    @refuse.setter
    def refuse(self, on: bool) -> None:
        """True stops accepting at once and drops every connection; False + ``await start()`` restores it."""
        self._refuse = on
        if on:
            self._stop_listening()

    def _stop_listening(self) -> None:
        # Synchronous on purpose: a plain attribute assignment must take effect before the next connect.
        if self._site is not None and self._site._server is not None:
            self._site._server.close()
        for transport in self._transports.values():
            if transport is not None:
                transport.close()
        self._listening = False

    async def start(self) -> int:
        if self._listening or self._refuse:
            return self.port
        if self._runner is not None:
            await self._runner.cleanup()
        app = web.Application()
        app.router.add_get("/api/v0.1", self._ws)  # ASSUMPTION Z-ENDPOINT
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        server = self._site._server
        assert server is not None
        self.port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        self._listening = True
        return self.port

    async def stop(self) -> None:
        for ws in self._sockets:
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._listening = False

    async def drop_connections(self) -> None:
        """Close every client connection, as a Core restart would."""
        for ws in self._sockets:
            await ws.close()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        frame = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        for ws in self._sockets:
            if not ws.closed:
                await ws.send_str(frame)

    # --- library and state ---------------------------------------------------------------------
    def by_id(self, media_id: int) -> dict[str, Any] | None:
        return next((m for m in self.media if m["mediaId"] == media_id), None)

    def available(self, system_id: str) -> bool:
        return any(ln.get("systemId", "").lower() == system_id.lower() and ln.get("available") for ln in self.launchers)

    def _start_game(self, media: dict[str, Any]) -> dict[str, Any]:
        self._end_active()
        self.active = {
            "mediaId": media["mediaId"],
            "launcherId": media["system"]["id"],
            "systemId": media["system"]["id"],
            "systemName": media["system"]["name"],
            "mediaPath": media["path"],  # ASSUMPTION Z-HISTORY-PATH: equals the indexed path
            "mediaName": media["name"],
            "started": now(),
            "zapScript": media["zapScript"],
        }
        return self.active

    def _end_active(self) -> dict[str, Any] | None:
        """Back to the menu. A running game becomes a history entry, as Zaparoo records it."""
        stopped, self.active = self.active, None
        if stopped:
            ended = datetime.now(UTC)
            started = datetime.fromisoformat(stopped["started"])
            self.history.insert(
                0,
                {
                    **{k: stopped[k] for k in ("mediaId", "systemId", "systemName", "mediaName", "mediaPath")},
                    "startedAt": stopped["started"],
                    "endedAt": ended.isoformat(timespec="seconds"),
                    "playTime": max(0, int((ended - started).total_seconds())),
                },
            )
        return stopped

    async def play(self, media_id: int) -> None:
        """Someone picks a game from the MiSTer's own menu; Zaparoo notices."""
        media = self.by_id(media_id)
        if media is None:
            raise KeyError(media_id)
        active = self._start_game(media)
        await self.notify("media.started", {k: active[k] for k in ("systemId", "systemName", "mediaPath", "mediaName")})

    async def quit_game(self) -> None:
        """Back to the menu (Zaparoo's stop, or the player quitting): history entry, then notification."""
        stopped = self._end_active()
        if stopped:  # ASSUMPTION Z-STOP-MENU: media.stopped follows
            await self.notify(
                "media.stopped",
                {
                    **{k: stopped[k] for k in ("systemId", "systemName", "mediaName", "mediaPath", "launcherId")},
                    "elapsed": self.history[0]["playTime"],
                },
            )

    def reindex(self) -> None:
        """A rebuilt media database: every row id changes. Call ``finish_indexing`` to announce it."""
        for m in self.media:
            m["mediaId"] += 1000

    async def finish_indexing(self) -> None:
        # ASSUMPTION Z-INDEXING: completion is a media.indexing notification with indexing false
        await self.notify("media.indexing", {"exists": True, "indexing": False, "totalMedia": len(self.media)})

    # --- transport -----------------------------------------------------------------------------
    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._transports[ws] = request.transport
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if msg.data == "ping":
                    await ws.send_str("pong")
                    continue
                task = asyncio.create_task(self._answer(ws, msg.data))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            self._transports.pop(ws, None)
        return ws

    async def _answer(self, ws: web.WebSocketResponse, text: str) -> None:
        try:
            req = json.loads(text)
        except ValueError:
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}}))
            return
        method, params, req_id = req.get("method"), req.get("params"), req.get("id")
        self.received.append((method, params))
        if self.silent:
            return
        if method == self.close_after:
            await ws.close()
            return
        try:
            if self.require_encryption:
                raise RpcError("encryption required", code=-32002)
            result = await self._call(str(method), params if isinstance(params, dict) else {})
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except RpcError as exc:
            err: dict[str, Any] = {"code": exc.code, "message": str(exc)}
            if exc.data:
                err["data"] = exc.data
            reply = {"jsonrpc": "2.0", "id": req_id, "error": err}
        if not ws.closed:
            await ws.send_str(json.dumps(reply))

    # --- methods -------------------------------------------------------------------------------
    async def _call(self, method: str, p: dict[str, Any]) -> Any:
        if method == "version":
            return {"version": "2.20.0", "platform": "mister"}
        if method == "media":
            return {
                "database": {"exists": True, "indexing": False, "totalMedia": len(self.media)},
                "active": [self.active] if self.active else [],
            }
        if method == "media.active":
            return self.active
        if method == "media.search":
            return self._search(p)
        if method == "media.meta":
            return self._meta(p)
        if method == "media.image":
            return self._image(p)
        if method == "media.history":
            return {"entries": self._history(p)}
        if method == "systems":
            return {"systems": self._systems(bool(p.get("all")))}
        if method == "launchers":
            return {"launchers": self.launchers}
        if method == "run":
            return await self._run(str(p.get("text") or ""))
        if method == "stop":
            await self.quit_game()  # ASSUMPTION Z-STOP-MENU
            return None
        raise RpcError(f"method not found: {method}", code=-32601)

    def _search(self, p: dict[str, Any]) -> dict[str, Any]:
        # ASSUMPTION Z-SEARCH-FIELDS: results carry mediaId, name, system, path (relativePath optional)
        words = str(p.get("query") or "").lower().split()
        systems = {s.lower() for s in p.get("systems") or []}
        rows = [
            m
            for m in self.media
            if all(w in m["name"].lower() or w in m["path"].lower() for w in words)
            and (not systems or m["system"]["id"].lower() in systems)
        ]
        cursor = p.get("cursor")
        if cursor is None:
            offset = 0
        elif isinstance(cursor, str) and cursor.isascii() and cursor.isdigit():
            offset = int(cursor)
        else:
            raise RpcError("invalid cursor", code=-32602)
        size = max(1, min(int(p.get("maxResults") or 100), 1000))
        if self.page_cap is not None:
            size = min(size, self.page_cap)
        page = rows[offset : offset + size]
        more = offset + size < len(rows)
        results = []
        for m in page:
            row = {k: m[k] for k in ("mediaId", "name", "system", "path")}
            if self.search_relative_path:
                row["relativePath"] = m["relativePath"]  # ASSUMPTION C-RELPATH
            results.append(row)
        return {
            "results": results,
            "pagination": {"nextCursor": str(offset + size) if more else None, "hasNextPage": more, "pageSize": size},
        }

    @staticmethod
    def _meta_of(m: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": m["path"],
            "relativePath": m["relativePath"],
            "zapScript": m["zapScript"],
            "isMissing": bool(m.get("isMissing")),
            "title": {"name": m["name"], "system": m["system"]},
        }

    def _meta(self, p: dict[str, Any]) -> dict[str, Any]:
        if "items" in p:
            items = p["items"]
            if not isinstance(items, list) or len(items) > MAX_META_BATCH:
                raise RpcError("invalid items", code=-32602)
            out = []
            for it in items:
                m = self.by_id(it.get("mediaId", -1)) if isinstance(it, dict) else None
                out.append(
                    {"mediaId": it.get("mediaId") if isinstance(it, dict) else None, "media": m and self._meta_of(m)}
                )
            return {"items": out}
        m = self.by_id(p.get("mediaId", -1))
        if m is None:
            raise RpcError("media not found")
        return {"media": self._meta_of(m)}

    def _image(self, p: dict[str, Any]) -> dict[str, Any]:
        m = self.by_id(p.get("mediaId", -1))
        if m is None:
            raise RpcError("media not found")
        if not m.get("image"):  # ASSUMPTION Z-IMAGE: an error when there is no art
            raise RpcError("no image found")
        return {"data": base64.b64encode(tiny_png()).decode(), "contentType": "image/png"}

    def _history(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        limit = max(1, min(int(p.get("limit") or 25), MAX_HISTORY))
        entries = self.history
        if p.get("distinctMedia"):
            seen: set[str] = set()
            entries = []
            for e in self.history:  # newest first, so the first sight of a path is its newest entry
                if e["mediaPath"] not in seen:
                    seen.add(e["mediaPath"])
                    entries.append(e)
        return entries[:limit]

    def _systems(self, every: bool) -> list[dict[str, Any]]:
        out = []
        for s in self.systems:
            if every or self.available(s["id"]):
                out.append({**s, "mediaCount": sum(1 for m in self.media if m["system"]["id"] == s["id"])})
        return out

    async def _run(self, text: str) -> None:
        # ASSUMPTION Z-RUN-WAITS: the reply comes after the launch
        if self.run_delay:
            await asyncio.sleep(self.run_delay)
        if self.run_error == "launch_repair":
            raise _launch_repair()
        if self.run_error:
            raise RpcError(f"run failed: {self.run_error}", data={"category": self.run_error})
        media = next((m for m in self.media if m["zapScript"] == text), None)
        if media is None:
            raise RpcError("media not found", data={"category": "media_not_found"})
        if not self.available(media["system"]["id"]):
            raise _launch_repair()
        active = self._start_game(media)
        await self.notify("media.started", {k: active[k] for k in ("systemId", "systemName", "mediaPath", "mediaName")})
        return None
