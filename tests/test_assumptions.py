"""Keep the assumption registry, the code and the docs in step.

ASSUMPTIONS is the source of truth (spec 13). This fails when:
- an id is missing from the README's assumptions table or from HARDWARE_VALIDATION.md
- the README table, HARDWARE_VALIDATION.md or the code cite an id that isn't in the registry
- an id is cited nowhere in src as `# ASSUMPTION <id>`
Docs are scoped to table rows, backticked ids and headings, so prose like "C-3PO" can't false-fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
ID_BODY = r"(?:Z|C|L)-[A-Z0-9]+(?:-[A-Z0-9]+)*"
ID = re.compile(rf"\b{ID_BODY}\b")
BACKTICKED = re.compile(rf"`({ID_BODY})`")
ROW = re.compile(rf"^\| `({ID_BODY})` \| (.+?) \| (\w+) \|$", re.M)
CITATION = re.compile(rf"ASSUMPTION ({ID_BODY})")

ASSUMPTIONS: tuple[tuple[str, str], ...] = (
    (
        "Z-LEGACY",
        "MiSTer Core admits unauthenticated plaintext LAN clients for every allow-listed method (incl. `media.image`)",
    ),
    ("Z-ENDPOINT", "`ws://<host>:7497/api/v0.1`, string ids, `ping` answered with `pong`"),
    (
        "Z-SEARCH-FIELDS",
        "`media.search` results carry `mediaId`, `name`, `systemId`, `path` (and maybe `relativePath`)",
    ),
    ("C-RELPATH", "`relativePath` is system-folder-relative and identical across devices on one share"),
    ("Z-RUN-WAITS", "`run` returns after the launch, within 30 s"),
    ("Z-STOP-MENU", "`stop` returns the MiSTer to the menu and `media.stopped` follows"),
    ("Z-HISTORY-PATH", "`media.history.mediaPath` and `media.active.mediaPath` equal the indexed path"),
    ("Z-INDEXING", "`media.indexing` signals completion in a detectable way"),
    (
        "Z-IMAGE",
        "`media.image` inline returns base64 and `contentType`, and an error or empty result when there is no art",
    ),
    ("Z-ENC-ERROR", "The encryption-required rejection is error code `-32002`"),
    ("L-THUMB-URL", "libretro-thumbnails URL and character-replacement rule"),
)
IDS = {i for i, _ in ASSUMPTIONS}
DOCS = ("README.md", "HARDWARE_VALIDATION.md")


def doc_ids(name: str) -> set[str]:
    """Ids a doc names deliberately: backticked ids and ids in headings."""
    text = (ROOT / name).read_text()
    found = set(BACKTICKED.findall(text))
    for line in text.splitlines():
        if line.startswith("#"):
            found.update(ID.findall(line))
    return found


def test_ids_are_unique_and_well_formed():
    ids = [i for i, _ in ASSUMPTIONS]
    assert len(ids) == len(set(ids)) == 11
    assert all(ID.fullmatch(i) for i in ids)


@pytest.mark.parametrize("doc", DOCS)
def test_every_assumption_is_in_each_doc(doc):
    assert doc_ids(doc) >= IDS, IDS - doc_ids(doc)


@pytest.mark.parametrize("doc", DOCS)
def test_docs_cite_only_real_ids(doc):
    assert doc_ids(doc) <= IDS, doc_ids(doc) - IDS


def test_readme_table_matches_the_registry():
    rows = ROW.findall((ROOT / "README.md").read_text())
    assert [(i, c) for i, c, _ in rows] == list(ASSUMPTIONS)
    assert {s for _, _, s in rows} <= {"unverified", "verified"}


def test_every_id_has_its_own_checklist_section():
    headings = [ln for ln in (ROOT / "HARDWARE_VALIDATION.md").read_text().splitlines() if ln.startswith("## ")]
    for i in IDS:
        assert any(i in ID.findall(h) for h in headings), f"no '## ' heading for {i}"


def test_code_cites_only_real_ids_and_every_id_is_used():
    cited = {m for p in sorted((ROOT / "src").rglob("*.py")) for m in CITATION.findall(p.read_text())}
    assert cited <= IDS, cited - IDS
    assert cited >= IDS, f"assumptions nothing cites: {IDS - cited}"
