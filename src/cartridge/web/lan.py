"""The LAN app: read routes, write routes and the guards that stand in for authentication (spec 9.1, 9.5).

The LAN side has no login, so every request must look like it came straight from a browser on this
network: no proxy headers, a Host we serve, and for writes a JSON content type, the ``X-Cartridge``
header (which forces a CORS preflight no page of ours answers) and a same-site Origin. No route or
middleware ever sets ``Access-Control-*``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import math
import socket
import uuid
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from cartridge.catalog import SyncRateLimited
from cartridge.core import Core
from cartridge.errors import ApiError
from cartridge.ids import is_game_id
from cartridge.launch import LaunchRequest
from cartridge.web.common import (
    EXCEPTION_HANDLERS,
    PING_SECONDS,
    SecurityHeadersMiddleware,
    read_routes,
)

log = logging.getLogger(__name__)

FORWARDED_HEADERS = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-real-ip",
        b"true-client-ip",
        b"cf-connecting-ip",
        b"cf-ray",
        b"via",
    }
)
SAFE_METHODS = frozenset({"GET", "HEAD"})
WILDCARD_BINDS = frozenset({"", "0.0.0.0", "::"})
MAX_BODY = 4096

LAN_WRITE_ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/launch"),
    ("POST", "/api/devices/{name}/menu"),
    ("POST", "/api/devices/{name}/sync"),
    ("POST", "/api/favorites/{id}"),
    ("DELETE", "/api/favorites/{id}"),
]


def default_lan_hosts(bind_host: str) -> frozenset[str]:
    """Names this machine answers to (spec Decisions): used when LAN_HOSTS isn't set."""
    hostname = socket.gethostname().lower()
    names = {"localhost", "127.0.0.1", "::1", hostname, f"{hostname}.local", socket.getfqdn().lower()}
    bind = bind_host.strip("[]").lower()
    if bind not in WILDCARD_BINDS:
        names.add(bind)
    with contextlib.suppress(OSError):
        names.update(str(info[4][0]).lower() for info in socket.getaddrinfo(hostname, None))
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # picks the outbound interface; UDP connect sends nothing
        names.add(probe.getsockname()[0])
    return frozenset(n.split("%")[0] for n in names if n)  # drop IPv6 zone ids


def parse_host(value: str) -> str | None:
    """The lowercase host of a Host header value with its port removed, or None when malformed."""
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        host, rest = text[1:end], text[end + 1 :]
        if end < 0 or not (rest == "" or (rest[0] == ":" and rest[1:].isascii() and rest[1:].isdigit())):
            return None
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
        return host
    host, sep, port = text.partition(":")
    if (sep and not (port.isascii() and port.isdigit())) or not host:
        return None
    return host


def parse_origin_host(origin: str) -> str | None:
    """The host of an ``http(s)://host[:port]`` Origin; anything else (including ``null``) is None."""
    try:
        parts = urlsplit(origin)
        _ = parts.port  # raises on a bad port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        return None
    return parts.hostname


class LanGuard:
    def __init__(self, app: ASGIApp, hosts: frozenset[str]) -> None:
        self.app = app
        self.hosts = frozenset(h.lower() for h in hosts)

    @staticmethod
    def _refuse(message: str) -> JSONResponse:
        return JSONResponse({"code": "bad_request", "message": message}, 403)

    def _check(self, scope: Scope) -> str | None:
        headers = Headers(scope=scope)
        if any(name in FORWARDED_HEADERS for name, _ in scope["headers"]):
            return "Requests through a proxy aren't served on the LAN address."
        host = parse_host(headers["host"]) if "host" in headers else None
        if host is None or host not in self.hosts:
            return "That host name isn't served here; use the address Cartridge was set up with."
        if scope["method"] in SAFE_METHODS:
            return None
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return "Writes must be sent as application/json."
        if headers.get("x-cartridge") != "1":
            return "Writes must come from the Cartridge page."
        origin = headers.get("origin")
        if origin is not None and parse_origin_host(origin) not in self.hosts:
            return "Writes from another site aren't allowed."
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websocket routes exist; refuse rather than skip the checks
            await send({"type": "websocket.close", "code": 1008})
            return
        problem = self._check(scope)
        if problem is not None:
            await self._refuse(problem)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# -- request bodies -------------------------------------------------------------------------------


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


async def _read_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise _bad("That request body is too large.")
    return bytes(body)


def _parse_object(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:  # includes bad UTF-8
        raise _bad("The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise _bad("The request body must be a JSON object.")
    return data


async def _json_object(request: Request) -> dict[str, Any]:
    return _parse_object(await _read_body(request))


async def _no_body(request: Request) -> None:
    """These routes take nothing; an empty body or ``{}`` is fine."""
    raw = await _read_body(request)
    if raw.strip() and _parse_object(raw):
        raise _bad("This request takes no body.")


def _launch_request(data: dict[str, Any]) -> LaunchRequest:
    if set(data) != {"device", "game", "replace", "request_id"}:
        raise _bad("A launch needs exactly device, game, replace and request_id.")
    device, game, replace, rid = data["device"], data["game"], data["replace"], data["request_id"]
    if not isinstance(device, str) or not isinstance(game, str) or not isinstance(replace, bool):
        raise _bad("device and game must be text and replace must be true or false.")
    try:
        if not isinstance(rid, str) or len(rid) != 36:
            raise ValueError
        uuid.UUID(rid)
    except ValueError:
        raise _bad("request_id must be a UUID.") from None
    return LaunchRequest(device, game, replace, rid)


def write_routes(core: Core) -> list[BaseRoute]:
    syncs: set[asyncio.Task[object]] = set()  # keeps fire-and-forget syncs alive until they finish

    async def launch(request: Request) -> Response:
        result = await core.launcher.launch(_launch_request(await _json_object(request)))
        return JSONResponse(result)

    async def menu(request: Request) -> Response:
        await _no_body(request)
        return JSONResponse(await core.launcher.return_to_menu(request.path_params["name"]))

    async def sync(request: Request) -> Response:
        await _no_body(request)
        device = request.path_params["name"]
        task: asyncio.Task[object] = asyncio.ensure_future(core.catalog.sync_now(device))
        syncs.add(task)
        task.add_done_callback(syncs.discard)
        await asyncio.sleep(0)  # the run's first step takes the rate-limit token or fails on an unknown device
        if task.done():
            try:
                task.result()
            except SyncRateLimited as exc:
                raise ApiError(429, "rate_limited", str(exc), retry_after=max(1, math.ceil(exc.wait))) from None
        else:
            task.add_done_callback(_log_failure)
        return JSONResponse({"device": device, "started": True}, 202)

    async def favorite(request: Request) -> Response:
        await _no_body(request)
        game_id = request.path_params["id"]
        on = request.method == "POST"
        if not is_game_id(game_id) or not core.catalog.set_favorite(game_id, on):
            raise ApiError(404, "unknown_game", "That game isn't in the catalog.")
        return JSONResponse({"game": game_id, "favorite": on})

    return [
        Route("/api/launch", launch, methods=["POST"]),
        Route("/api/devices/{name}/menu", menu, methods=["POST"]),
        Route("/api/devices/{name}/sync", sync, methods=["POST"]),
        Route("/api/favorites/{id}", favorite, methods=["POST", "DELETE"]),
    ]


def _log_failure(task: asyncio.Future[object]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("A requested sync ended with an unexpected error.", exc_info=task.exception())


def build_lan_app(core: Core, hosts: frozenset[str], *, ping_seconds: float = PING_SECONDS) -> Starlette:
    return Starlette(
        routes=[*read_routes(core, "lan", ping_seconds=ping_seconds), *write_routes(core)],
        middleware=[Middleware(SecurityHeadersMiddleware), Middleware(LanGuard, hosts=hosts)],
        exception_handlers=EXCEPTION_HANDLERS,  # type: ignore[arg-type]
    )
