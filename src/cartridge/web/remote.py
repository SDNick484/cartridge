"""The remote app: the read routes only, behind Cloudflare Access (spec 4, 9.4).

It has no write routes at all, so a write is a 404 whoever asks. There is no LanGuard: the app sits
behind cloudflared, so forwarded headers and any Host are expected. The security headers are outermost,
so an Access refusal carries them too; ``/healthz`` is answered by the Access layer before auth.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.types import ASGIApp

from cartridge.access import AccessMiddleware, AccessVerifier
from cartridge.core import Core
from cartridge.web.common import EXCEPTION_HANDLERS, PING_SECONDS, SecurityHeadersMiddleware, read_routes


def build_remote_app(core: Core, verifier: AccessVerifier, *, ping_seconds: float = PING_SECONDS) -> ASGIApp:
    async def ready() -> bool:
        return core.db.ping()

    app = Starlette(
        routes=read_routes(core, "remote", ping_seconds=ping_seconds), exception_handlers=EXCEPTION_HANDLERS
    )
    return SecurityHeadersMiddleware(AccessMiddleware(app, verifier, health_check=ready))
