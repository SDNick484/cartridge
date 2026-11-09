"""Test helpers shared by the web tests: signed Access tokens, raw ASGI scopes and polling."""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from cartridge.access import AccessPolicy, AccessVerifier

TEAM = "team.cloudflareaccess.com"
AUD = "aud-tag"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
W = {"Content-Type": "application/json", "X-Cartridge": "1"}


def token(*, aud: str = AUD, exp_in: int = 600) -> str:
    now = int(time.time())
    claims = {"aud": [aud], "iss": f"https://{TEAM}", "iat": now - 5, "exp": now + exp_in}
    return jwt.encode(claims, KEY, algorithm="RS256")


def jwt_header(**kw: Any) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": token(**kw)}


async def _resolve(_token: str) -> Any:
    return KEY.public_key()


def make_verifier() -> AccessVerifier:
    return AccessVerifier(AccessPolicy(TEAM, (AUD,)), key_resolver=_resolve)


def scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("192.0.2.50", 5000),
        "server": ("games.example.com", 80),
    }


async def wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")
