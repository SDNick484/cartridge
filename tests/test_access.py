import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cartridge.access import AccessMiddleware, AccessPolicy, AccessVerifier, policy_from_settings
from cartridge.config import load_settings
from cartridge.errors import ConfigError

pytestmark = pytest.mark.anyio

TEAM = "team.cloudflareaccess.com"
AUD = "aud-tag"
FORBIDDEN = {"code": "forbidden", "message": "Sign in through Cloudflare Access to view this."}

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(*, key: Any = KEY, aud: str = AUD, iss: str = f"https://{TEAM}", exp_in: int = 600) -> str:
    now = int(time.time())
    claims = {"aud": [aud], "iss": iss, "iat": now - 5, "exp": now + exp_in, "email": "Me@Example.com"}
    return jwt.encode(claims, key, algorithm="RS256")


async def resolve(_token: str) -> Any:
    return KEY.public_key()


async def healthy() -> bool:
    return True


def make_client(
    emails: frozenset[str] = frozenset(),
    health: Callable[[], Awaitable[bool]] = healthy,
) -> httpx.AsyncClient:
    async def index(request: Request) -> JSONResponse:
        return JSONResponse({"hello": "world"})

    app = Starlette(routes=[Route("/", index), Route("/healthz", index, methods=["GET", "POST"])])
    verifier = AccessVerifier(AccessPolicy(TEAM, (AUD,), emails), key_resolver=resolve)
    transport = httpx.ASGITransport(app=AccessMiddleware(app, verifier, health_check=health))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def hdr(t: str) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": t}


def assert_forbidden(r: Any) -> None:
    assert r.status_code == 403
    assert r.json() == FORBIDDEN
    assert r.headers["content-type"] == "application/json"
    assert not [h for h in r.headers if h.startswith("access-control")]


async def test_no_jwt_is_forbidden() -> None:
    assert_forbidden(await make_client().get("/"))


@pytest.mark.parametrize(
    "bad",
    [
        token(aud="other"),
        token(iss="https://evil.example.com"),
        token(exp_in=-120),
        token(key=OTHER_KEY),
        "not-a-jwt",
    ],
    ids=["aud", "issuer", "expired", "signature", "garbage"],
)
async def test_bad_tokens_are_forbidden(bad: str) -> None:
    assert_forbidden(await make_client().get("/", headers=hdr(bad)))


async def test_email_not_allowed_is_forbidden() -> None:
    client = make_client(frozenset({"someone@example.com"}))
    assert_forbidden(await client.get("/", headers=hdr(token())))


async def test_email_allowed_case_insensitively() -> None:
    client = make_client(frozenset({"me@example.com"}))
    assert (await client.get("/", headers=hdr(token()))).status_code == 200


async def test_valid_token_passes() -> None:
    r = await make_client().get("/", headers=hdr(token()))
    assert r.status_code == 200
    assert r.json() == {"hello": "world"}


async def test_within_leeway_passes() -> None:
    assert (await make_client().get("/", headers=hdr(token(exp_in=-10)))).status_code == 200


async def test_health_needs_no_jwt() -> None:
    r = await make_client().get("/healthz")
    assert r.status_code == 200
    assert r.content == b'{"status": "ok"}'
    assert (await make_client().head("/healthz")).status_code == 200


async def test_health_not_ready() -> None:
    async def no() -> bool:
        return False

    r = await make_client(health=no).get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready"}


async def test_health_check_exception_is_503_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def boom() -> bool:
        raise RuntimeError("db gone")

    with caplog.at_level(logging.WARNING):
        r = await make_client(health=boom).get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready"}
    assert "Health check failed" in caplog.text


async def test_health_post_needs_a_jwt() -> None:
    assert_forbidden(await make_client().post("/healthz"))


async def test_denial_log_never_contains_token(caplog: pytest.LogCaptureFixture) -> None:
    t = token(key=OTHER_KEY)
    with caplog.at_level(logging.INFO):
        await make_client().get("/", headers=hdr(t))
    assert "Denied GET /" in caplog.text
    assert t not in caplog.text


async def test_lifespan_passes_through() -> None:
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    verifier = AccessVerifier(AccessPolicy(TEAM, (AUD,)), key_resolver=resolve)
    mw = AccessMiddleware(app, verifier, health_check=healthy)
    await mw({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


ENV = {"CF_ACCESS_TEAM_DOMAIN": f"https://{TEAM}/", "CF_ACCESS_AUD": "a1, a2", "REMOTE_ALLOWED_EMAILS": "A@x.com"}


def test_policy_from_settings() -> None:
    p = policy_from_settings(load_settings(ENV))
    assert p.team_domain == TEAM
    assert p.audiences == ("a1", "a2")
    assert p.emails == frozenset({"a@x.com"})
    assert p.issuer == f"https://{TEAM}"
    assert "a@x.com" not in repr(p)


def test_policy_from_settings_refuses_missing() -> None:
    s = load_settings(ENV)
    with pytest.raises(ConfigError):
        policy_from_settings(replace(s, cf_access_aud=()))
    with pytest.raises(ConfigError):
        policy_from_settings(replace(s, cf_access_team_domain=None))
