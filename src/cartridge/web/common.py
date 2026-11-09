"""Pieces both web apps share: error shape, security headers and the read routes (spec 9)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from importlib import resources

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, ExceptionHandler, Message, Receive, Scope, Send

from cartridge.core import Core
from cartridge.errors import ApiError
from cartridge.events import Event, TooManyClients
from cartridge.ids import is_game_id
from cartridge.web.dto import (
    GamePageDTO,
    Mode,
    config_dto,
    device_dto,
    devices_dto,
    game_dto,
    recent_dto,
    sync_event_dto,
    system_dto,
)

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; connect-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
INTERNAL_MESSAGE = "Something went wrong; see the server log."
PING_SECONDS = 15.0
RECENT_LIMIT = 50
DEFAULT_LIMIT = 120
MAX_LIMIT = 500
ART_FOUND_CACHE = "public, max-age=604800"
ART_PLACEHOLDER_CACHE = "public, max-age=3600"  # a recorded miss expires sooner so later-found art shows up
ART_TRANSIENT_CACHE = "no-cache"  # the sources couldn't tell: revalidate (the ETag makes it cheap) until they can

# -- errors and headers ---------------------------------------------------------------------------


def error_response(err: ApiError) -> JSONResponse:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after is not None else None
    return JSONResponse({**err.extra, "code": err.code, "message": err.message}, err.status, headers)


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status, headers)


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return error_response(exc)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    headers = dict(exc.headers) if exc.headers else None
    if exc.status_code == 404:
        return _error(404, "not_found", "There is nothing at that address.", headers)
    if exc.status_code == 405:
        return _error(405, "method_not_allowed", "That method isn't allowed here.", headers)
    return _error(exc.status_code, "bad_request", "That request couldn't be handled.", headers)


EXCEPTION_HANDLERS: dict[type[Exception], ExceptionHandler] = {
    ApiError: _api_error,
    HTTPException: _http_error,
}


class SecurityHeadersMiddleware:
    """Adds the fixed security headers to every HTTP response and turns a crash into a clean 500."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def wrapped(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers[key] = value
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            log.exception("Unhandled error in a web request.")  # the traceback stays in the log
            if not started:
                await _error(500, "internal", INTERNAL_MESSAGE)(scope, receive, wrapped)


# -- read routes ----------------------------------------------------------------------------------

_UINT = re.compile(r"[0-9]{1,9}")


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _flag(request: Request, name: str, default: bool) -> bool:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if raw in ("1", "true"):
        return True
    if raw in ("0", "false"):
        return False
    raise _bad(f"{name} must be 1 or 0.")


def _int(request: Request, name: str, default: int, low: int, high: int | None) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    value = int(raw) if _UINT.fullmatch(raw) else -1
    if value < low or (high is not None and value > high):
        bounds = f"{low} or more" if high is None else f"{low} to {high}"
        raise _bad(f"{name} must be a whole number from {bounds}.")
    return value


def _etag_matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    tags = [t.strip().removeprefix("W/") for t in header.split(",")]
    return "*" in tags or etag in tags


def read_routes(core: Core, mode: Mode, *, ping_seconds: float = PING_SECONDS) -> list[BaseRoute]:
    async def index(request: Request) -> Response:
        page = resources.files("cartridge").joinpath("static", "index.html")
        return HTMLResponse(page.read_bytes())

    async def healthz(request: Request) -> Response:
        if core.db.ping():
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "not_ready"}, 503)

    async def config(request: Request) -> Response:
        return JSONResponse(config_dto(core, mode))

    async def devices(request: Request) -> Response:
        return JSONResponse(devices_dto(core))

    async def systems(request: Request) -> Response:
        return JSONResponse([system_dto(s) for s in core.catalog.systems()])

    async def games(request: Request) -> Response:
        params = request.query_params
        device = params.get("device") or None
        offset = _int(request, "offset", 0, 0, None)
        try:
            page = core.catalog.games(
                system=params.get("system") or None,
                q=params.get("q") or None,
                device=device,
                only_device=_flag(request, "only_device", device is not None),
                favorites=_flag(request, "favorites", False),
                show_unavailable=_flag(request, "show_unavailable", False),
                offset=offset,
                limit=_int(request, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
            )
        except KeyError:  # catalog raises it only for an unconfigured device
            raise ApiError(404, "unknown_device", "That device isn't configured.") from None
        return JSONResponse(GamePageDTO(games=[game_dto(i) for i in page.items], total=page.total, offset=offset))

    async def game(request: Request) -> Response:
        game_id = request.path_params["id"]
        item = core.catalog.game(game_id) if is_game_id(game_id) else None
        if item is None:
            raise ApiError(404, "unknown_game", "That game isn't in the catalog.")
        return JSONResponse(game_dto(item))

    async def recent(request: Request) -> Response:
        return JSONResponse([recent_dto(r) for r in core.catalog.recent(RECENT_LIMIT)])

    async def art(request: Request) -> Response:
        game_id = request.path_params["id"]
        result = await core.art.get(game_id) if is_game_id(game_id) else None
        if result is None:
            raise ApiError(404, "unknown_game", "That game isn't in the catalog.")
        cache = ART_FOUND_CACHE if result.found else ART_TRANSIENT_CACHE if result.transient else ART_PLACEHOLDER_CACHE
        headers = {"ETag": result.etag, "Cache-Control": cache}
        if _etag_matches(request.headers.get("if-none-match"), result.etag):
            return Response(status_code=304, headers=headers)
        return Response(result.body, media_type=result.content_type, headers=headers)

    def sse_frame(event: str, data: object) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

    def frame_for(event: Event) -> bytes | None:
        kind = str(event.get("type", "message"))
        if kind == "device":
            try:  # the whole card, so the UI needn't refetch
                return sse_frame(kind, device_dto(core, str(event.get("device"))))
            except KeyError:
                return None
        if kind in ("sync.started", "sync.finished"):
            return sse_frame(kind, sync_event_dto(event))
        log.debug("Dropped an unknown hub event type: %r", kind)
        return None

    async def stream() -> AsyncIterator[bytes]:
        try:
            async with core.hub.subscribe(mode) as events:
                yield sse_frame("hello", devices_dto(core))
                pending = asyncio.ensure_future(anext(events))
                try:
                    while True:
                        # wait() leaves the read running on timeout; wait_for would cancel it and end the stream
                        await asyncio.wait({pending}, timeout=ping_seconds)
                        if not pending.done():
                            yield b": ping\n\n"
                            continue
                        try:
                            event = pending.result()
                        except StopAsyncIteration:  # the hub closed
                            return
                        pending = asyncio.ensure_future(anext(events))
                        if (frame := frame_for(event)) is not None:
                            yield frame
                finally:
                    pending.cancel()
        except TooManyClients:  # lost a race for the last slot after the pre-check passed
            return

    async def events(request: Request) -> Response:
        if core.hub.client_count(mode) >= core.hub.max_clients:
            raise ApiError(503, "too_many_clients", f"Too many {mode} clients are connected; close another tab.")
        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    routes: list[BaseRoute] = [
        Route("/", index),
        Mount("/static", StaticFiles(packages=[("cartridge", "static")])),
        Route("/api/config", config),
        Route("/api/devices", devices),
        Route("/api/systems", systems),
        Route("/api/games", games),
        Route("/api/games/{id}", game),
        Route("/api/recent", recent),
        Route("/api/events", events),
        Route("/art/{id}", art),
    ]
    if mode == "lan":
        routes.append(Route("/healthz", healthz))
    return routes
