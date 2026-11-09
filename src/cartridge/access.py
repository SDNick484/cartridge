"""Cloudflare Access verification for the read-only remote app (spec 9.4).

Local copy pending the access.py split in mcp-server-shared (spec §11).

Cloudflare Access sits in front of the tunnel and attaches a signed JWT to every request it lets
through. The remote app fails closed: anything without a valid assertion (signature against the
team's published keys, audience, issuer, expiry, optional e-mail allow-list) gets a 403, so the
app is safe even when reached from the LAN, where nothing passes through Access. ``/healthz`` is
answered before auth for local monitors and says nothing beyond ok / not_ready.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio
import jwt
from starlette.types import ASGIApp, Receive, Scope, Send

from cartridge.config import Settings
from cartridge.errors import ConfigError

log = logging.getLogger(__name__)

ACCESS_HEADER = "cf-access-jwt-assertion"
HEALTH_PATH = "/healthz"
FORBIDDEN_BODY = {"code": "forbidden", "message": "Sign in through Cloudflare Access to view this."}

KeyResolver = Callable[[str], Awaitable[Any]]


class AccessDenied(Exception):
    """A request's Access assertion is missing or doesn't check out."""


@dataclass(frozen=True)
class AccessPolicy:
    """Which Cloudflare Access application's assertions to accept."""

    team_domain: str  # "example.cloudflareaccess.com"
    audiences: tuple[str, ...]  # the application's AUD tag(s)
    # Empty: anyone Access let through. repr=False: a repr in a log or traceback must not list who is allowed in.
    emails: frozenset[str] = field(default=frozenset(), repr=False)

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"


def policy_from_settings(settings: Settings) -> AccessPolicy:
    """The policy for the remote app; refuses to build one that would accept anything."""
    team = re.sub(r"^https?://", "", (settings.cf_access_team_domain or "").strip(), flags=re.IGNORECASE).rstrip("/")
    if not team or not settings.cf_access_aud:
        raise ConfigError(
            "The remote app needs CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD; set them to your Cloudflare Access values."
        )
    if not re.fullmatch(r"[A-Za-z0-9.-]+", team):
        raise ConfigError(
            "CF_ACCESS_TEAM_DOMAIN must be a hostname such as example.cloudflareaccess.com, without a path or port."
        )
    return AccessPolicy(
        team_domain=team,
        audiences=tuple(settings.cf_access_aud),
        emails=frozenset(e.lower() for e in settings.remote_allowed_emails),
    )


class AccessVerifier:
    """Checks a ``Cf-Access-Jwt-Assertion`` against an AccessPolicy.

    The signing keys come from the team's published certs endpoint. PyJWT's PyJWKClient caches them
    and refetches when a token names an unknown key id, which is how Cloudflare's key rotation is
    picked up. Its fetch is blocking, so it runs in a worker thread. Tests pass their own
    ``key_resolver``.
    """

    leeway = 30  # seconds of clock skew tolerated on exp/iat
    min_refetch_interval = 60.0  # minimum seconds between JWKS network fetches on unknown kid

    def __init__(self, policy: AccessPolicy, key_resolver: KeyResolver | None = None) -> None:
        self.policy = policy
        if key_resolver is None:
            client = jwt.PyJWKClient(policy.certs_url, cache_jwk_set=True, lifespan=3600, timeout=10)
            lock = anyio.Lock()
            last_fetch: float | None = None

            async def resolve(token: str) -> Any:  # pragma: no cover
                nonlocal last_fetch
                async with lock:
                    header = jwt.get_unverified_header(token)
                    kid = header.get("kid")
                    now = time.monotonic()
                    if client.jwk_set_cache is not None and not client.jwk_set_cache.is_expired():
                        cached_set = client.jwk_set_cache.get()
                        if cached_set is not None:
                            cached_kids = {k.key_id for k in cached_set.keys}
                            if (
                                kid not in cached_kids
                                and last_fetch is not None
                                and (now - last_fetch) < self.min_refetch_interval
                            ):
                                raise jwt.PyJWKClientError(f"Unable to find a signing key that matches {kid!r}")
                    try:
                        key = await anyio.to_thread.run_sync(client.get_signing_key_from_jwt, token)
                    finally:
                        last_fetch = time.monotonic()
                return key.key

            key_resolver = resolve
        self._resolve = key_resolver

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            key = await self._resolve(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=list(self.policy.audiences),
                issuer=self.policy.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessDenied(f"invalid Access assertion: {exc}") from exc
        if self.policy.emails:
            email = str(claims.get("email", "")).lower()
            if email not in self.policy.emails:
                # Somebody Access let through but this app doesn't want. Mask the address: logsafe
                # doesn't redact e-mail addresses.
                local, _, domain = email.partition("@")
                shown = f"{local[:1]}***@{domain}" if domain else "this identity"
                raise AccessDenied(f"{shown} is not in REMOTE_ALLOWED_EMAILS")
        return claims


async def _json(send: Send, status: int, body: Mapping[str, Any]) -> None:
    data = json.dumps(dict(body)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": data})


class AccessMiddleware:
    """Lets a request through only with a valid Access assertion; unknown scope types are denied.

    Lifespan passes untouched. Http and websocket both need the assertion (a failed websocket is
    closed with 1008 before the app sees it); any other scope type never reaches the app. The health
    check is answered before auth.
    """

    def __init__(self, app: ASGIApp, verifier: AccessVerifier, *, health_check: Callable[[], Awaitable[bool]]) -> None:
        self.app = app
        self.verifier = verifier
        self.health_check = health_check

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind not in ("http", "websocket"):
            log.warning("Denied unknown scope type %r", kind)
            return
        if kind == "http" and scope["path"] == HEALTH_PATH and scope.get("method") in ("GET", "HEAD"):
            await self._health(send)
            return
        headers = dict(scope.get("headers") or [])
        token = headers.get(ACCESS_HEADER.encode(), b"").decode("latin-1")
        try:
            if not token:
                raise AccessDenied("no Cloudflare Access assertion (request didn't come through Access)")
            await self.verifier.verify(token)
        except AccessDenied as exc:
            log.info("Denied %s %s: %s", scope.get("method", kind), scope["path"], exc)
            await self._deny(kind, send)
            return
        except Exception as exc:  # a failing key fetch must deny, not 500
            log.warning("Access verification failed (%s); denying %s", type(exc).__name__, scope["path"])
            await self._deny(kind, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(kind: str, send: Send) -> None:
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await _json(send, 403, FORBIDDEN_BODY)

    async def _health(self, send: Send) -> None:
        try:
            ready = await self.health_check()
        except Exception as exc:
            log.warning("Health check failed (%s)", type(exc).__name__)
            ready = False
        if ready:
            await _json(send, 200, {"status": "ok"})
        else:
            await _json(send, 503, {"status": "not_ready"})
