"""Game identity (spec 6.2): the same ROM gets the same id on every device and across rescans."""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from collections.abc import Sequence

GAME_ID_RE = re.compile(r"^[a-z2-7]{16}$")
_SLASHES = re.compile(r"/{2,}")
_ARTICLES = ("the ", "a ")


def normalize_rel_path(path: str) -> str:
    p = unicodedata.normalize("NFC", path.replace("\\", "/"))
    return _SLASHES.sub("/", p).lstrip("/")


def game_id(system_id: str, rel_path: str) -> str:
    digest = hashlib.sha256((system_id + "\0" + normalize_rel_path(rel_path)).encode("utf-8")).digest()
    return base64.b32encode(digest[:10]).decode("ascii").lower()


def is_game_id(s: str) -> bool:
    return GAME_ID_RE.fullmatch(s) is not None


def rel_path_from_abs(abs_path: str, strip_prefixes: Sequence[str]) -> str | None:
    """Drop the longest matching prefix, then the system folder; None when no prefix matches."""
    # ASSUMPTION C-RELPATH: used only when relativePath differs between devices sharing a library.
    path = normalize_rel_path(abs_path)
    best: str | None = None
    for prefix in strip_prefixes:
        norm = normalize_rel_path(prefix).rstrip("/")
        if (path == norm or path.startswith(norm + "/")) and (best is None or len(norm) > len(best)):
            best = norm
    if best is None:
        return None
    rest = path[len(best) :].lstrip("/")
    _system, sep, rel = rest.partition("/")
    return rel if sep and rel else None


def sort_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c)).strip()
    for article in _ARTICLES:
        if t.startswith(article) and len(t) > len(article):
            return t[len(article) :].lstrip()
    return t
