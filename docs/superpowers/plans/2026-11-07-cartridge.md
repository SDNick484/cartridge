# Cartridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-hosted web app that shows a MiSTer game library as a box-art grid and launches games on an explicitly chosen device through Zaparoo Core, with a read-only remote view behind Cloudflare Access.

**Architecture:** One async Python process (Starlette + uvicorn) serves two ASGI apps on two sockets: a LAN app (browse + write routes, Host/Origin/CSRF guards) and a remote app built from a route table with no write routes (Access JWT on every request). Both share one core: SQLite catalog, `DeviceRegistry` of `LaunchTarget`s (`ZaparooTarget` over a JSON-RPC WebSocket client), art cache and SSE `EventHub`. Everything is built and tested against `FakeZaparoo`, a real JSON-RPC WebSocket server.

**Tech Stack:** Python ≥ 3.11, Starlette, uvicorn, aiohttp (Zaparoo client + fake server), httpx (libretro art, tests), pyjwt[crypto], PyYAML, sqlite3 (WAL, FTS5), pytest + anyio plugin, ruff, mypy strict, plain-JS ES modules, Playwright (Chromium) for UI smoke.

**Spec:** `docs/superpowers/specs/2026-11-07-cartridge-design.md` (the source of truth; section numbers below are its §s). Read the spec section a task names before starting it.

**Reference siblings (read-only, cloned next to this repo; never edit them):**
- `../mcp-server-misterfpga/src/misterfpga_mcp/zaparoo.py` — client the local `zaparoo_client.py` derives from.
- `../mcp-server-misterfpga/src/misterfpga_mcp/sim/{fake_zaparoo,device,harness}.py` — fake the local `sim/` derives from.
- `../mcp-server-shared/src/mcp_shared/remote.py` — `AccessPolicy`/`AccessVerifier`/`AccessMiddleware` the local `access.py` derives from.
- `../mcp-server-shared/scripts/sync_shared.py`, `../mcp-server-misterfpga/shared.lock`, `../mcp-server-misterfpga/.github/workflows/ci.yml` — sync layout and CI shape.
- `../mcp-server-misterfpga/tests/test_assumptions.py`, `deploy/alpine/*` — patterns to mirror.

## Global Constraints

- Python `>=3.11`; package `cartridge` under `src/cartridge/`; console script `cartridge = "cartridge.cli:main"`; `__version__ = "0.1.0"`.
- Runtime deps exactly: `starlette`, `uvicorn`, `aiohttp`, `httpx`, `pyjwt[crypto]`, `pyyaml`. Dev: `pytest`, `anyio`, `ruff`, `mypy`, `playwright`, `types-PyYAML`.
- ruff select `E, W, F, I, UP, B, SIM`, line length 120, `ruff format`; mypy `strict = true` on `src`.
- Run every tool as `python -m …` inside the repo venv (`. .venv/bin/activate`).
- **Verification gate**, run before every commit and completion claim:
  `python -m pytest && python -m ruff check . && python -m ruff format --check . && python -m mypy && python scripts/check_js.py`
- Async tests use the anyio pytest plugin (`pytestmark = pytest.mark.anyio`, `anyio_backend` fixture returns `"asyncio"`), never pytest-asyncio.
- No hardware, no internet in tests: devices are `FakeZaparoo` on loopback; httpx goes through `httpx.MockTransport`.
- **Generic by design:** no personal names, hostnames or domains anywhere. Examples use `example.com`, `192.0.2.x`, devices `family-room` / `office`.
- Never edit `src/cartridge/{logsafe,tasks,ratelimit}.py`, `tests/test_{logsafe,tasks,ratelimit}.py`, `scripts/sync_shared.py`, `shared.lock` by hand.
- Zaparoo allow-list (§9.3) exactly: `version, media, media.search, media.meta, media.image, media.active, media.history, systems, launchers, run, stop`. Never `unsafe`, never `media.generate`.
- Every unverified protocol claim is cited in code as `# ASSUMPTION <id>` using only the §13 ids: `Z-LEGACY, Z-ENDPOINT, Z-SEARCH-FIELDS, C-RELPATH, Z-RUN-WAITS, Z-STOP-MENU, Z-HISTORY-PATH, Z-INDEXING, Z-IMAGE, Z-ENC-ERROR, L-THUMB-URL`.
- Errors reach the UI as `{"code": str, "message": str}`; `message` is a sentence a person can act on.
- Logs go to stderr via `logsafe.setup_logging`; never log request bodies.
- Commits: `git -c user.email=SDNick484@users.noreply.github.com` (set once in the worktree's local config), conventional messages, ending with the `Co-Authored-By` trailer from the session.

## Decisions this plan makes (where the spec is silent)

- **Shared sync excludes more than `remote`.** mcp-server-shared now also ships `registry`, `lookup`, `tools`; `tools.py` imports `mcp`, and `registry` would overwrite Cartridge's own `registry.py`. §11 names only `logsafe`, `tasks`, `ratelimit`, so `shared.lock` has `exclude = ["remote", "registry", "lookup", "tools"]`.
- **`LaunchTarget.launch(ref, *, replace: bool = False)`.** §7.3 orders meta re-read → stop → run; the target owns all three Zaparoo calls, so `replace` is a keyword on `launch`. `TargetRef` carries the `rel_path` it was issued for so the target can detect a stale ref (`StaleRef`).
- **`strip_prefixes`:** the longest matching prefix is removed from `abs_path`, then the first remaining segment (the system folder) is dropped, so `/media/fat/games/SNES/a.sfc` and `/userdata/roms/snes/a.sfc` both give `a.sfc`. When set it overrides `relativePath`.
- **`media.meta` batch shape** (Z-SEARCH-FIELDS): request `{"items": [{"mediaId": n}, …]}` (≤ 100), result `{"items": [{"mediaId": n, "media": {…} | null}]}`.
- **Availability** of a system on a device = some `launchers` entry has that `systemId` and `available: true`.
- **`LAN_HOSTS` default** with a wildcard bind (`0.0.0.0`/`::`): `localhost`, `127.0.0.1`, `::1`, `socket.gethostname()`, `<hostname>.local`, `socket.getfqdn()`, every address `getaddrinfo(hostname)` returns, and the primary outbound address (UDP `connect` to `192.0.2.1`, no packet sent). With a specific bind: that address plus the same names. Compared case-insensitively, port ignored.
- **Rate-limit refunds:** a launch/menu token taken at check 7 is refunded when the request is then rejected at 8, 9 or 10 (nothing reached the device).
- **libretro `<Name>`** is the basename of `rel_path` without its extension.
- **Placeholder tiles** are served with `Cache-Control: public, max-age=3600` (found art keeps `604800`), so later-found art shows up within an hour.
- **Games API is paged** (`offset`, `limit` default 120, max 500) and the grid loads more on scroll.
- **`frontend-design` skill is not installed in this environment.** The UI task specifies the palette tokens directly instead.
- **Bundled font:** Inter variable (`InterVariable.woff2`, OFL-1.1) from the rsms/inter v4.1 release, with `OFL.txt`.
- `DEVICES_FILE` default `/etc/cartridge/devices.yaml`; `LOG_LEVEL` default `INFO`; `simulate` defaults `REMOTE_ENABLED=0`.

## Review Focus

1. **Awkward titles and paths** — `&`, `:`, accents, NFD filenames from SMB, and search input like `"`, `*`, `AND`, `c:` must give stable ids and a 200, never an FTS syntax 500. Pinned in Tasks 2, 3 and 13.
2. **A device dropping mid-operation** — the socket closes during a sync page or during `run`: the sync run is `failed` with no `present` flips, the launch returns a sentence (503/504) and writes a `launches` row. Pinned in Tasks 8 and 10.
3. **Browser with no device chosen or no storage** — a launch without `device`, with an unknown key, or with a non-bool `replace` is a 400 and reaches no device. Pinned in Task 13.
4. **Host header variants** — `Host: 192.0.2.5:8770`, uppercase names, `[::1]:8770` match the allow-list ignoring port and case; anything else 403. Pinned in Task 13.
5. **SSE churn** — the 51st client gets 503 `too_many_clients`; a disconnected client frees its slot; shutdown ends open streams. Pinned in Tasks 9 and 13.

---

## File map

```
pyproject.toml  LICENSE  .gitignore  CLAUDE.md  README.md  HARDWARE_VALIDATION.md
shared.lock  scripts/sync_shared.py (synced)  scripts/check_js.py
.github/workflows/ci.yml
deploy/alpine/{install.sh,cartridge.initd,run.sh,conf.d.example,devices.yaml.example,smoke-test.sh}
src/cartridge/
  __init__.py  errors.py  config.py  ids.py  db.py  zaparoo_client.py  registry.py
  events.py  catalog.py  art.py  launch.py  access.py  core.py  cli.py
  logsafe.py tasks.py ratelimit.py            (synced, never edited)
  targets/{__init__,base,zaparoo}.py
  web/{__init__,common,lan,remote}.py
  sim/{__init__,fake_zaparoo,harness}.py  sim/fixtures/{family_room,office}.json
  static/{index.html,app.js,api.js,grid.js,devices.js,focus.js,style.css}  static/fonts/{InterVariable.woff2,OFL.txt}
tests/
  conftest.py  test_{logsafe,tasks,ratelimit}.py (synced)
  test_ids.py test_config.py test_db.py test_fake_zaparoo.py test_zaparoo_client.py test_safety.py
  test_target_zaparoo.py test_registry.py test_events.py test_sync.py test_history.py test_queries.py
  test_launch.py test_art.py test_access.py test_web_lan.py test_web_remote.py test_leaks.py
  test_core.py test_cli.py test_assumptions.py
  e2e/test_ui.py
```

`errors.py` holds `ApiError` (shared by `launch.py` and `web/`); `core.py` composes the core and runs the background loops (startup/periodic/indexing syncs, history ingest, event fan-out). Neither is in §5's table; both exist so `cli.py` and `web/` stay thin.

---

### Task 1: Scaffold, CLAUDE.md, shared sync, CI, gate

**Files:**
- Create: `pyproject.toml`, `LICENSE` (MIT, "Cartridge contributors"), `.gitignore` (`.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `/sim-data/`), `CLAUDE.md`, `src/cartridge/__init__.py`, `scripts/check_js.py`, `tests/__init__.py`, `tests/conftest.py`, `shared.lock`, `.github/workflows/ci.yml`
- Synced (by script, not by hand): `scripts/sync_shared.py`, `src/cartridge/{logsafe,tasks,ratelimit}.py`, `tests/test_{logsafe,tasks,ratelimit}.py`

**Interfaces:**
- Produces: `cartridge.__version__: str = "0.1.0"`; `tests/conftest.py::anyio_backend` fixture; synced `cartridge.logsafe.setup_logging(level, *, redacted=True)`, `cartridge.tasks.{spawn_task, stop_task, stop_tasks}`, `cartridge.ratelimit.{TokenBucket, KeyedTokenBucket, refusal}`.

- [ ] **Step 1: pyproject.** setuptools build; deps and dev extras per Global Constraints (`starlette>=0.40`, `uvicorn>=0.31`, `aiohttp>=3.9`, `httpx>=0.27`, `pyjwt[crypto]>=2.10`, `pyyaml>=6`; dev `pytest>=8`, `anyio>=4`, `ruff>=0.6`, `mypy>=1.11`, `playwright>=1.45`, `types-PyYAML`). `[tool.setuptools.package-data] cartridge = ["sim/fixtures/*.json", "static/*", "static/fonts/*"]`. pytest: `testpaths = ["tests"]`, `markers = ["e2e: Playwright UI smoke (run with -m e2e)"]`, `addopts = "-m 'not e2e'"`. ruff as constraints, `src = ["src", "tests"]`, `extend-exclude = ["*.md"]`. mypy `files = ["src"]`, `strict = true`, `[[tool.mypy.overrides]] module = "cartridge.sim.*"` stays strict (no overrides).

- [ ] **Step 2: Shared sync bootstrap.** Copy `../mcp-server-shared/scripts/sync_shared.py` to `scripts/sync_shared.py` byte for byte. Write `shared.lock`:
  ```toml
  # (the LOCK_HEADER comment block from sync_shared.py)
  repo = "SDNick484/mcp-server-shared"
  commit = "3ccb5f1e1139abb708c113f6663e64eb953cb702"
  package = "cartridge"
  exclude = ["remote", "registry", "lookup", "tools"]
  ```
  Run `python scripts/sync_shared.py` (fetches that commit from GitHub). Expected: writes the three modules and three tests. Then `python scripts/sync_shared.py --check` → exit 0.

- [ ] **Step 3: `scripts/check_js.py`.** Stdlib only. For each `src/cartridge/static/*.js` (sorted), pipe the file to `node --input-type=module --check` on stdin; print `ok <name>` or the error; exit 1 if any failed, 0 otherwise (also 0 when there are no files). Exit 2 with a sentence if `node` isn't on PATH.

- [ ] **Step 4: venv and gate.** `python3.11 -m venv .venv && . .venv/bin/activate && python -m pip install -e ".[dev]"`. Run the gate. Expected: synced tests pass; ruff/format/mypy clean; `check_js` exit 0.

- [ ] **Step 5: CLAUDE.md.** Sections (concise, imperative):
  - *What this is*: one paragraph + "spec is the source of truth; read it before changing behavior".
  - *Module map*: the File map above, one line per module with its job.
  - *Guardrails* (from §9/§11/§13): no paths, refs or hosts to the browser (DTOs are TypedDicts, `test_leaks` enforces); devices only from config and only by name — **never guess a device**, the server always requires `device`; launch validation order §7.3 with zero device calls on rejection; the Zaparoo allow-list (exact set, `test_safety` pins it, widening is a spec change); remote app has no write routes and fails closed on Access; LAN app refuses proxied requests, enforces Host allow-list and the write guards, never sends CORS headers; strict CSP, no inline script, no CDNs; logs to stderr via logsafe, never request bodies; shared files are synced, never edited (`python scripts/sync_shared.py --update`, CI `shared-drift`); every unverified protocol claim is `# ASSUMPTION <id>` and listed in README and HARDWARE_VALIDATION.md (`test_assumptions`); generic by design (no personal names/hosts/domains); no `media.generate`, no API key, legacy access only.
  - *Verification gate*: the exact command; "run it before every commit and every completion claim"; e2e: `python -m pytest -m e2e` (needs `python -m playwright install chromium`).
  - *Commands*: venv setup, `cartridge simulate`, `cartridge check`, `python scripts/sync_shared.py --check`.

- [ ] **Step 6: CI** (`.github/workflows/ci.yml`), shaped like misterfpga's: triggers `push: [main]`, `pull_request`, weekly `schedule` (`0 8 * * 1`), `workflow_dispatch`. Job `test` (matrix 3.11–3.14; `actions/setup-node@v4` node 22; install; ruff check; ruff format --check; mypy; pytest; `python scripts/check_js.py`). Job `shared-drift` (copy misterfpga's verbatim). Jobs `alpine` and `e2e` are added in Tasks 16/17.

- [ ] **Step 7: Gate, commit.** `git add -A && git commit -m "chore: scaffold project, CLAUDE.md, shared sync and CI"`

---

### Task 2: `ids.py` and `config.py`

Spec: §6.1, §6.2, §14 settings table.

**Files:** Create `src/cartridge/ids.py`, `src/cartridge/config.py`, `src/cartridge/errors.py`; Test `tests/test_ids.py`, `tests/test_config.py`

**Interfaces:**
- Produces (`ids.py`): `GAME_ID_RE = re.compile(r"^[a-z2-7]{16}$")`; `normalize_rel_path(path: str) -> str` (backslashes → `/`, NFC, strip leading `/`, collapse `//`); `game_id(system_id: str, rel_path: str) -> str` (`base32(sha256(system_id + "\0" + normalize_rel_path(rel_path))[:10]).lower()`); `is_game_id(s: str) -> bool`; `rel_path_from_abs(abs_path: str, strip_prefixes: Sequence[str]) -> str | None` (Decisions rule; `None` when no prefix matches); `sort_title(title: str) -> str` (casefold, NFKD with combining marks removed, leading `"the "` / `"a "` moved off).
- Produces (`errors.py`): `class ConfigError(ValueError)`; `class ApiError(Exception)` with `__init__(self, status: int, code: str, message: str, *, extra: dict[str, object] | None = None, retry_after: int | None = None)`.
- Produces (`config.py`): frozen dataclasses
  - `DeviceConfig(name: str, label: str, model: str, platform: Literal["mister"], transport: Literal["zaparoo"], host: str, port: int = 7497, auth: Literal["legacy"] = "legacy", strip_prefixes: tuple[str, ...] = ())`
  - `DeviceProblem(index: int, name: str | None, label: str, sentence: str)` — `name` set only when the entry has a valid, unique name (so it can get a "misconfigured" card)
  - `Devices(devices: tuple[DeviceConfig, ...], problems: tuple[DeviceProblem, ...])`
  - `Settings(lan_bind: tuple[str, int], lan_hosts: frozenset[str] | None, remote_enabled: bool, remote_bind: tuple[str, int], cf_access_team_domain: str | None, cf_access_aud: tuple[str, ...], remote_allowed_emails: frozenset[str], data_dir: Path, devices_file: Path, art_online: bool, sync_interval_hours: float, log_level: str)`
  - `load_settings(env: Mapping[str, str]) -> Settings` (raises `ConfigError` listing every problem, one sentence each); `load_devices(path: Path) -> Devices` (raises `ConfigError` only for a missing/unreadable/non-YAML file or a top level without a `devices` list); `parse_bind(text: str, default_port: int) -> tuple[str, int]` (accepts `host:port`, `[v6]:port`, bare host).

- [ ] **Step 1: Failing tests** (`tests/test_ids.py`):
  - `test_game_id_shape`: `is_game_id(game_id("SNES", "Super Metroid (USA).sfc"))`, length 16.
  - `test_same_rom_same_id_regardless_of_form`: `game_id("SNES","a/b.sfc") == game_id("SNES","/a/b.sfc") == game_id("SNES","a\\b.sfc")`; NFD `"Façade"` (`"Façade"`) equals NFC `"Façade"`.
  - `test_system_is_part_of_identity`: `game_id("SNES","x.bin") != game_id("Genesis","x.bin")`.
  - `test_rel_path_from_abs`: prefixes `("/media/fat/games/",)` → `"/media/fat/games/SNES/Sub/a.sfc"` gives `"Sub/a.sfc"`; longest prefix wins with `("/media/", "/media/fat/games/")`; no match → `None`.
  - `test_sort_title`: `sort_title("The Legend of Zelda") == "legend of zelda"`, `sort_title("Écran") == "ecran"`.
  - `test_is_game_id_rejects`: `"ABCDEFGHIJKLMNOP"`, `"a"*15`, `"0"*16`, `"../../etc/passwd"` all False.

  (`tests/test_config.py`, writing YAML to `tmp_path`):
  - `test_valid_devices_load` with the §6.1 example: two devices, `port` default 7497, `label`/`model` read.
  - `test_bad_entry_does_not_block_others`: entries (ok `office`; `name: Bad Name!`; duplicate `office`; `port: 70000`; `host: "not a host!"`; `platform: batocera`; `auth: paired`) → `devices == (office,)`, five-plus problems, each `sentence` names the entry and the fix (assert substrings: `"[a-z0-9-]"`, `"already used"`, `"1–65535"`, `"batocera isn't supported in v1"`, `"auth: paired isn't implemented yet"`).
  - `test_problem_keeps_name_when_valid`: the batocera entry named `cabinet` has `problem.name == "cabinet"`; the `Bad Name!` one has `name is None`.
  - `test_missing_file_is_config_error`, `test_not_a_devices_list_is_config_error`.
  - `test_settings_defaults`: `load_settings({"REMOTE_ENABLED": "0"})` → `lan_bind == ("0.0.0.0", 8770)`, `remote_bind == ("127.0.0.1", 8771)`, `data_dir == Path("/var/lib/cartridge")`, `devices_file == Path("/etc/cartridge/devices.yaml")`, `art_online is True`, `sync_interval_hours == 6`.
  - `test_remote_enabled_without_access_refuses`: `load_settings({})` raises `ConfigError` whose message contains `"CF_ACCESS_TEAM_DOMAIN"` and `"REMOTE_ENABLED=0"`.
  - `test_settings_parse`: `LAN_HOSTS="a.example.com, B.example.com"` → `{"a.example.com","b.example.com"}`; `CF_ACCESS_AUD="x,y"` → `("x","y")`; `REMOTE_ALLOWED_EMAILS` lowercased; `LAN_BIND="[::]:9000"` → `("::", 9000)`; `SYNC_INTERVAL_HOURS="abc"` → `ConfigError`.

- [ ] **Step 2: Run** `python -m pytest tests/test_ids.py tests/test_config.py -v` → FAIL (import errors).
- [ ] **Step 3: Implement** the three modules. Host validation: `ipaddress.ip_address` or hostname regex `^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$`. Name `^[a-z0-9-]{1,32}$`. `label` defaults to `name`, `model` to `""`. `strip_prefixes` must be absolute paths. Cite `# ASSUMPTION C-RELPATH` at `rel_path_from_abs`.
- [ ] **Step 4: Run** the tests → PASS; gate.
- [ ] **Step 5: Commit** `feat: game ids and config loading`

---

### Task 3: `db.py`

Spec: §6.3, §7.1 step 3, §6.2.

**Files:** Create `src/cartridge/db.py`; Test `tests/test_db.py`

**Interfaces:**
- Consumes: `ids.sort_title`.
- Produces: `class Database` with `open(path: Path) -> Database` (classmethod; creates parent dir, WAL, `foreign_keys=ON`, migrates via `PRAGMA user_version` to 1), `Database.memory() -> Database`, `close()`, `ping() -> bool`, and a context manager `transaction()` (BEGIN IMMEDIATE / COMMIT / ROLLBACK). Typed row dataclasses and methods (all synchronous; called from the event loop):
  - `upsert_system(id, name, manufacturer: str | None, release_date: str | None)`
  - `set_device_system(device, system_id, available: bool, media_count: int, synced_at: str)`
  - `upsert_game(game_id, system_id, title, now: str) -> bool` (True when newly inserted; keeps `first_seen`, updates `title`, `sort_title`, `updated_at`, FTS)
  - `upsert_device_media(device, game_id, media_ref: str, abs_path: str, synced_at: str)` (sets `present = 1`)
  - `mark_missing(device, before: str) -> int` (rows with `synced_at < before` → `present = 0`; returns count)
  - `start_sync_run(device, started_at) -> int`, `finish_sync_run(run_id, finished_at, status: Literal["ok","failed"], seen, added, missing, error: str | None)`, `last_sync(device) -> SyncRun | None`, `last_good_sync(device) -> SyncRun | None`
  - `device_media(device, game_id) -> DeviceMedia | None` (`media_ref`, `abs_path`, `present`), `game_id_for_path(device, abs_path) -> str | None`
  - `game(game_id) -> GameRow | None`, `query_games(*, system: str | None, q: str | None, present_on: str | None, favorites: bool, include_unavailable: bool, offset: int, limit: int) -> tuple[list[GameRow], int]`
  - `presence(game_ids: Sequence[str]) -> dict[str, dict[str, PresenceRow]]` (per game, per device: `present`, `system_available`)
  - `systems_with_games() -> list[SystemRow]` (only systems with ≥ 1 present game; with `game_count`)
  - `set_favorite(game_id, on: bool, now: str)`, `favorite_ids(game_ids) -> set[str]`
  - `upsert_history(device, entries: Sequence[HistoryRow])`, `recent_history(limit: int) -> list[HistoryRow]` (newest first across devices)
  - `record_launch(device, game_id, requested_at, outcome: str, detail: str | None)`, `last_ok_launch(device) -> LaunchRow | None`
  - `art(game_id) -> ArtRow | None`, `set_art(game_id, source, file: str | None, content_type: str | None, status: Literal["found","none"], fetched_at, retry_after: str | None)`
- Search: `fts_query(q: str) -> str | None` (module function) splits on whitespace, drops empty tokens, wraps each as `"tok"*` with `"` doubled, joins with spaces (implicit AND); `None` if no tokens. FTS5 table `games_fts(title, content='games', content_rowid='rowid', tokenize="unicode61 remove_diacritics 2")` kept in sync by triggers. Timestamps are ISO-8601 UTC strings passed in by callers.

- [ ] **Step 1: Failing tests** (`tests/test_db.py`, all on `Database.memory()`):
  - `test_migrates_to_version_1` (`PRAGMA user_version == 1`; reopening a file DB doesn't re-run).
  - `test_upsert_game_reports_new_once` and keeps `first_seen`.
  - `test_mark_missing_only_flips_unseen_on_that_device`.
  - `test_query_hides_games_present_nowhere_unless_asked`.
  - `test_query_present_on_and_system_filters` and `total` counts before paging; `offset/limit` pages in `sort_title` order.
  - `test_search_matches_accents_and_prefix`: title `"Façade & Friends: Part II"` found by `q="facade"`, `q="fri"`, `q="Part II"`.
  - `test_search_survives_fts_syntax` (Review Focus 1): `q` in `['"', '*', 'AND', 'c:', 'NEAR(', "'", '-']` → no exception.
  - `test_transaction_rolls_back`.
  - `test_favorites_roundtrip`, `test_recent_history_merges_devices_newest_first`, `test_last_ok_launch_ignores_failures`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** `feat: SQLite schema and queries`

---

### Task 4: `sim/` — FakeZaparoo, fixtures, harness

Spec: §3, §13 Simulator. Derive from `../mcp-server-misterfpga/src/misterfpga_mcp/sim/fake_zaparoo.py` and `device.py` (MIT, same author): header comment says so.

**Files:** Create `src/cartridge/sim/{__init__,fake_zaparoo,harness}.py`, `src/cartridge/sim/fixtures/{family_room,office}.json`; Test `tests/test_fake_zaparoo.py`; Modify `tests/conftest.py`

**Interfaces:**
- Produces:
  - `FIXTURE_MEDIA_ROOT = "/media/fat/games"`; `load_fixture(name: str) -> dict[str, Any]`; `tiny_png(width=4, height=3) -> bytes`.
  - `class FakeZaparoo(fixture: dict[str, Any], host="127.0.0.1", port=0)`: `async start() -> int`, `async stop()`, `async drop_connections()`, `async notify(method, params)`, `async play(media_id)` (sends `media.started`), `async quit_game()` (history entry + `media.stopped`), `reindex()` (every `mediaId += 1000`, then `notify("media.indexing", {"exists": True, "indexing": False, "totalMedia": n})` is the caller's job via `async finish_indexing()`), `received: list[tuple[str, Any]]`, `methods: list[str]`, `active: dict | None`, `history: list[dict]`.
  - Failure switches (attributes): `refuse: bool` (stop listening; `start()` again restores), `require_encryption: bool` (every request → error `{"code": -32002, "message": "encryption required"}`), `run_delay: float`, `run_error: str | None` (category; `"launch_repair"` adds `data.reason = "launcher_plugin_missing"`), `silent: bool`, `page_cap: int | None` (caps `maxResults`), `search_relative_path: bool = True` (False omits `relativePath` from search results only), `close_after: str | None` (close the socket upon receiving that method, before answering).
  - `class Simulation(root: Path)`: `async add(fixture: str, port: int = 0) -> SimDevice` (`SimDevice(name: str, fake: FakeZaparoo)`), `async stop()`, `devices_yaml() -> str` (§6.1 format, host `127.0.0.1`, each fake's port), `write_devices(path: Path) -> Path`.
  - `tests/conftest.py`: fixture `sim` (Simulation with `family_room` then `office`), helpers `family(sim)`, `office(sim)`.
- Methods answered (shapes; cite the matching ASSUMPTION at each): `version` → `{"version": "2.20.0", "platform": "mister"}`; `media` → `{database:{exists,indexing,totalMedia}, active:[...]}`; `media.search {systems, maxResults, cursor}` → `{results:[{mediaId, name, system:{id,name}, path, relativePath?}], pagination:{nextCursor: str|None, hasNextPage, pageSize}}` (cursor = opaque string of the next offset); `media.meta {mediaId}` → `{media:{path, relativePath, zapScript, isMissing, title:{name, system}}}` or error `"media not found"`; `media.meta {items}` → batch shape (Decisions); `media.image {mediaId, ...}` → `{data: base64, contentType: "image/png"}` when the fixture item has `"image": true`, else error `"no image found"`; `media.active` → active or `null`; `media.history {limit, distinctMedia}` → `{entries:[...]}`; `systems {all}` → `{systems:[{id,name,manufacturer,releaseDate,mediaCount}]}` (all fixture systems when `all`, else only those with an available launcher); `launchers` → `{launchers:[...]}`; `run {text}` → `null` after `run_delay` (or error with `data.category = run_error`; unknown text → `media_not_found`), sends `media.started`; `stop` → `quit_game()`, `null`. Anything else → `-32601`. Text `ping` → `pong`.
- Fixtures (§13): media paths absolute under `/media/fat/games/<System>/`, `relativePath` relative to the system folder.
  - `family_room.json`: `name: "Family Room"`, `model: "SuperStation One"`; systems SNES, Genesis, NES; launchers available for all three. Media: *Super Metroid* (SNES, `image: true`), *Donkey Kong Country* (SNES), *Gunstar Heroes* (Genesis), *Sonic the Hedgehog* (Genesis), *Façade & Friends: Part II* (SNES; file `Façade & Friends - Part II (USA).sfc`, stored NFD in `path`/`relativePath`), *Family Only Quest* (NES; only here). History: two entries (one with `mediaId`, one NFC-card launch without `mediaId`).
  - `office.json`: `name: "Office"`, `model: "MiSTer Pi"`; systems SNES, Genesis, NES; launchers: SNES and NES available, **Genesis launcher absent** (core missing). Media: the same SNES and Genesis files as family-room (different `mediaId`s, e.g. 101…; Façade stored NFC), *Contra* (NES). History: one entry.

- [ ] **Step 1: Failing tests** (`tests/test_fake_zaparoo.py`, raw `aiohttp` client against `sim`): ping/pong; `media.search` with `page_cap=2` returns `nextCursor` and following it yields every SNES item exactly once; `media.meta` batch returns `null` for an unknown id; `require_encryption` → error code `-32002`; `run_error="busy"` → `data.category == "busy"`; `reindex()` changes every id and `media.meta` on an old id errors; `refuse` → connect raises `aiohttp.ClientConnectorError`; `systems {all: true}` on office includes Genesis while `launchers` has no Genesis entry; `received` records methods in order.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** `feat: simulated Zaparoo Core and fixtures`

---

### Task 5: `zaparoo_client.py` and the allow-list

Spec: §3, §9.3, §11. Derive from `../mcp-server-misterfpga/src/misterfpga_mcp/zaparoo.py`; header comment: "Local copy pending promotion to mcp-server-shared (spec §11); derived from mcp-server-misterfpga (MIT)." Drop tokens/NFC handling.

**Files:** Create `src/cartridge/zaparoo_client.py`; Test `tests/test_zaparoo_client.py`, `tests/test_safety.py`

**Interfaces:**
- Produces:
  - `API_PATH = "/api/v0.1"`; `ALLOWED_METHODS: frozenset[str]` (exact §9.3 set); `DEFAULT_TIMEOUT = 10.0`, `RUN_TIMEOUT = 35.0`, `PING_EVERY = 20.0`, `SILENCE_LIMIT = 60.0`, `BACKOFF_MAX = 30.0`; `ENCRYPTION_REQUIRED = -32002` (`# ASSUMPTION Z-ENC-ERROR`).
  - Exceptions: `ZaparooUnavailable(Exception)`; `ZaparooError(Exception)` with `.code: int | None`, `.category: str | None`, `.reason: str | None`; `EncryptionRequired(ZaparooError)`; `ZaparooTimeout(Exception)`.
  - `describe_failure(exc, label, host, port, setup_hint) -> str` (as sibling; "No answer from {label}: is it on and on the network?").
  - `class ZaparooClient(label: str, host: str, port: int, *, allowed_methods: frozenset[str] = ALLOWED_METHODS, setup_hint: str, transport: Literal["legacy"] = "legacy")`: `start()`, `stop()`, `connected: bool`, `version: str | None`, `error: str | None`, `needs_pairing: bool`, `tried: asyncio.Event`, `add_listener(cb: Callable[[str, dict[str, Any]], None])`, `add_state_listener(cb: Callable[[bool], None])`, `async call(method, params=None, timeout=DEFAULT_TIMEOUT) -> Any`.
  - `call` raises `ValueError` **before touching the socket** for a method outside `allowed_methods`, or when `params` contains the key `"unsafe"`.
  - On `-32002` during the version prime: `needs_pairing = True`, `error = "needs pairing (Zaparoo encryption is on)"`, state listeners hear `False`, and the client keeps retrying with backoff.

- [ ] **Step 1: Failing tests.**
  - `tests/test_safety.py`: `test_allow_list_is_exactly_the_spec` (literal set) and that `settings.update`, `device.power.reboot`, `update.apply`, `input.keyboard`, `media.generate`, `readers.write`, `clients.pair` are absent; `test_disallowed_method_raises_before_socket` (connected client; `call("media.generate")` → `ValueError`; fake's `received` unchanged); `test_unsafe_param_refused` (`call("run", {"text": "x", "unsafe": True})` → `ValueError`, nothing received).
  - `tests/test_zaparoo_client.py` (against `sim`): connects and reads version; concurrent calls matched by id; notification reaches listener; `drop_connections()` → state listener `False` then reconnects → `True`; error reply → `ZaparooError` with category/reason; `silent` + short timeout → `ZaparooTimeout`; `refuse` → `error` contains "isn't running" or "No answer"; `require_encryption` → `needs_pairing` and the sentence.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** `feat: Zaparoo JSON-RPC client with allow-list`

---

### Task 6: `targets/base.py` and `targets/zaparoo.py`

Spec: §5.1, §7.1–7.3, §12.

**Files:** Create `src/cartridge/targets/{__init__,base,zaparoo}.py`; Test `tests/test_target_zaparoo.py`

**Interfaces:**
- Produces (`base.py`), frozen dataclasses unless noted:
  - `TargetRef(token: str, rel_path: str)` — opaque outside the issuing target.
  - `NowPlaying(system_id: str, media_name: str, abs_path: str | None, started_at: str | None)`
  - `DeviceState = Literal["connecting", "online", "offline", "needs_pairing", "misconfigured"]`
  - `DeviceStatus(state: DeviceState, message: str | None, version: str | None, now_playing: NowPlaying | None)` with property `online -> bool`
  - `SystemAvailability(system_id, name, manufacturer: str | None, release_date: str | None, available: bool, media_count: int)`
  - `CatalogEntry(system_id, title, rel_path, abs_path, ref: TargetRef)`
  - `HistoryEntry(system_id, media_name, abs_path: str | None, started_at: str, play_time: int)`
  - `Image(data: bytes, content_type: str)`
  - `TargetEvent(device: str, kind: Literal["status", "media.started", "media.stopped", "indexing.finished"])`
  - `LaunchError(Exception)` with class attrs `code: ClassVar[str]`, `http_status: ClassVar[int]`, and subclasses: `Gone` (410 `gone`), `DeviceBusy` (409 `device_busy`), `Blocked` (409 `blocked`), `LaunchFailed` (502 `launch_failed`, `.reason: str | None`), `LaunchTimeout` (504 `timeout`), `Offline` (503 `offline`), `StaleRef` (internal; 410 `gone` if it escapes).
  - `class LaunchTarget(Protocol)`: §5.1 exactly, except `async def launch(self, ref: TargetRef, *, replace: bool = False) -> None`.
- Produces (`zaparoo.py`): `SETUP_HINT` (points at README "Install Zaparoo Core on a MiSTer"); `class ZaparooTarget(config: DeviceConfig, client: ZaparooClient | None = None)` implementing `LaunchTarget`, `name/model/platform` from config. Constants `SEARCH_PAGE = 500`, `META_BATCH = 100`, `STOP_WAIT = 5.0`.
  - `status()`: `connecting` until `client.tried`; `needs_pairing` when `client.needs_pairing`; `online`/`offline` with `client.error`.
  - On state up: `media.active` → `now_playing`, emit `status`. Notifications: `media.started` → now_playing from params, emit `media.started`; `media.stopped` → `None`, emit `media.stopped`; `media.indexing` with `indexing is False` → emit `indexing.finished` (`# ASSUMPTION Z-INDEXING`).
  - `systems()`: `systems {all: true}` + `launchers`; available = Decisions rule.
  - `catalog()`: for each system with `mediaCount > 0`, page `media.search {systems: [s], maxResults: SEARCH_PAGE, cursor}` until `nextCursor` is null; collect results lacking `relativePath` and resolve them with `media.meta {items}` in batches of `META_BATCH`; `rel_path` = `rel_path_from_abs` when `strip_prefixes` set, else `normalize_rel_path(relativePath)`; skip (log warning) when neither yields one. System id from `systemId` or `system.id` (`# ASSUMPTION Z-SEARCH-FIELDS`). `TargetRef(str(mediaId), rel_path)`.
  - `artwork(ref, max_size)`: §8 row 1 params; `None` on error or empty `data` (`# ASSUMPTION Z-IMAGE`).
  - `history(limit)`: `media.history {limit, distinctMedia: true}`.
  - `launch(ref, *, replace)`: `media.meta {mediaId}` → error → `StaleRef`; `rel_path` differs from `ref.rel_path` → `StaleRef`; `isMissing` → `Gone`; if `replace` and something is playing → `return_to_menu()`; `run {text: zapScript}` with `RUN_TIMEOUT` (`# ASSUMPTION Z-RUN-WAITS`) mapping categories `media_not_found→Gone`, `busy→DeviceBusy`, `blocked→Blocked`, `launch_repair→LaunchFailed(reason)`, `timeout`/`ZaparooTimeout→LaunchTimeout`, `ZaparooUnavailable→Offline`.
  - `return_to_menu()`: `stop`, then wait for a `media.stopped` notification or `media.active == null` (poll every 0.5 s) within `STOP_WAIT` (`# ASSUMPTION Z-STOP-MENU`), else `LaunchTimeout`.

- [ ] **Step 1: Failing tests** (`tests/test_target_zaparoo.py`, real client against `sim`):
  - `test_catalog_pages_and_yields_every_item` (`page_cap = 2`), entries carry relative `rel_path` and absolute `abs_path`.
  - `test_catalog_falls_back_to_meta_batch` (`search_relative_path = False`): same `rel_path`s; fake received `media.meta` with `items` of ≤ 100.
  - `test_strip_prefixes_overrides_relative_path`.
  - `test_systems_availability_from_launchers` (office: Genesis `available False`, `media_count > 0`).
  - `test_status_states` (connecting → online; `refuse` → offline with sentence; `require_encryption` → `needs_pairing`).
  - `test_now_playing_tracks_notifications` (`fake.play(id)` → `now_playing.media_name`; `quit_game()` → `None`; events emitted).
  - `test_launch_success`, `test_launch_stale_ref_after_reindex` → `StaleRef`, `test_launch_missing_file` → `Gone`, parametrized `test_run_error_categories` (`media_not_found, busy, blocked, launch_repair, timeout` → classes above; `LaunchFailed.reason == "launcher_plugin_missing"`).
  - `test_launch_replace_stops_first` (fake `methods` ends `[..., "media.meta", "stop", ..., "run"]`), `test_return_to_menu_times_out` (fake `silent` after stop; patch `STOP_WAIT` to 0.2).
  - `test_indexing_finished_event`.
  - `test_drop_during_run_is_offline` (Review Focus 2): `close_after = "run"` → `Offline`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; gate.
- [ ] **Step 5: Commit** `feat: LaunchTarget protocol and ZaparooTarget`

---

### Task 7: `registry.py`

Spec: §5 row, §6.1, §7.3 checks 2/7/9, §9.2.

**Files:** Create `src/cartridge/registry.py`; Test `tests/test_registry.py`

**Interfaces:**
- Consumes: `Devices`, `DeviceConfig`, `DeviceProblem`, `ZaparooTarget`, `LaunchTarget`, `KeyedTokenBucket`.
- Produces: `TargetFactory = Callable[[DeviceConfig], LaunchTarget]`; `DeviceInfo(name, label, model, platform, problem: str | None)`; `class DeviceRegistry(devices: Devices, factory: TargetFactory = ZaparooTarget, *, clock: Callable[[], float] = time.monotonic)` with `async start()`, `async close()`, `names() -> list[str]` (config order, incl. misconfigured-with-name), `info(name) -> DeviceInfo | None`, `target(name) -> LaunchTarget | None` (None for unknown or misconfigured), `status(name) -> DeviceStatus` (misconfigured → `DeviceStatus("misconfigured", "misconfigured: <sentence>", None, None)`), `subscribe(listener: Callable[[TargetEvent], None])` (fans in from every target), `lock(name) -> asyncio.Lock`, `actions: KeyedTokenBucket` (capacity 4, rate 1/15, shared by launch and menu), `problems: tuple[DeviceProblem, ...]`.
- There is no lookup by anything but exact configured name.

- [ ] **Step 1: Failing tests:** unknown name → `None`; lookup is exact (`"Office"`, `" office"`, `"office "` → None); misconfigured device with a valid name has `status().state == "misconfigured"` and no target; `actions` allows 4 then refuses with a wait ≈ 15 s (fake clock); locks are per device; subscribe receives events from a stub target; `start()` starts every target and a raising target's `start` doesn't stop the others.
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: device registry`

---

### Task 8: `events.py` and catalog sync

Spec: §7.1, §12 "Sync failed midway", §9.6.

**Files:** Create `src/cartridge/events.py`, `src/cartridge/catalog.py`; Test `tests/test_events.py`, `tests/test_sync.py`; Modify `tests/conftest.py` (fixtures `db`, `registry` started against `sim`, closed after)

**Interfaces:**
- Produces (`events.py`): `Event = dict[str, object]` with a `"type"` key; `class EventHub(max_clients: int = 50)`: `publish(event: Event) -> None` (non-blocking; per-client queue size 100, oldest dropped), `subscribe(pool: Literal["lan", "remote"]) -> contextlib.AbstractAsyncContextManager[AsyncIterator[Event]]` raising `TooManyClients` when that pool already has `max_clients` (§9.6: 50 per app), `close()` (ends every open subscription), `client_count(pool) -> int`.
- Produces (`catalog.py`): `SyncResult(device, status: Literal["ok","failed"], seen, added, missing, error: str | None)`; `class SyncRateLimited(Exception)` with `.wait: float`; `class Catalog(db: Database, registry: DeviceRegistry, hub: EventHub, *, now: Callable[[], datetime] = utcnow, clock=time.monotonic)`:
  - `async sync(device: str) -> SyncResult` — joins a run already in flight for that device (same `asyncio.Task`); publishes `{"type": "sync.started", "device"}` and `{"type": "sync.finished", "device", "status"}`.
  - `async sync_now(device: str) -> SyncResult` — `sync` gated by a per-device `KeyedTokenBucket(capacity=1, rate=1/60)`; joining a running sync doesn't take a token; refused → `SyncRateLimited`.
  - Run steps exactly §7.1: in one `db.transaction()`: systems + `device_systems`; every `CatalogEntry` → `upsert_game(game_id(system_id, rel_path), …)` + `upsert_device_media`; `mark_missing(device, run_started)`; on any exception roll back and record a `failed` run with the error sentence (`str(exc)` of `ZaparooUnavailable` etc.).

- [ ] **Step 1: Failing tests.**
  - `tests/test_events.py`: publish reaches two subscribers; 51st `lan` subscribe → `TooManyClients` while a `remote` subscribe still succeeds; leaving the context frees the slot (`client_count`); `close()` ends iteration; a slow subscriber drops oldest, doesn't block.
  - `tests/test_sync.py` (against `sim`):
    - `test_identity_across_devices` (§13 must-have): after syncing both, *Super Metroid* and *Façade & Friends* (NFD on family-room, NFC on office) each have one `games` row present on both devices.
    - `test_identity_stable_across_reindex`: `reindex()` + sync → same `game_id`s, updated `media_ref`.
    - `test_pagination` (`page_cap = 2`) syncs every item.
    - `test_present_flips` (remove a media item from the fake's fixture → `present = 0` there, row kept).
    - `test_failed_run_changes_nothing` (Review Focus 2): `close_after = "media.search"` → `SyncResult.status == "failed"`, `device_media`/`present` unchanged, `sync_runs` row `failed` with an error sentence, `last_good_sync` still the earlier run.
    - `test_availability_from_systems_all`: office Genesis `available = 0`, `media_count > 0`.
    - `test_concurrent_sync_joins` (two `sync()` calls → one `systems` call on the fake).
    - `test_sync_now_rate_limited` (second `sync_now` within 60 s after the first finished → `SyncRateLimited`).
    - `test_sync_publishes_events`.
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: event hub and catalog sync`

---

### Task 9: History, now-playing and catalog queries

Spec: §7.2, §6.3 "Last launched", §10 grid/game sheet data.

**Files:** Modify `src/cartridge/catalog.py`; Test `tests/test_history.py`, `tests/test_queries.py`

**Interfaces:**
- Produces on `Catalog`:
  - `async ingest_history(device: str) -> None` — `target.history(50)` → `db.upsert_history`, resolving `game_id` via `db.game_id_for_path(device, abs_path)` (`# ASSUMPTION Z-HISTORY-PATH`); offline → no-op.
  - `resolve_now_playing(device: str) -> ResolvedNowPlaying | None` — `ResolvedNowPlaying(title: str, system_id: str, game_id: str | None, started_at: str | None)`; title is the game's title when resolved, else `media_name`.
  - `recent(limit: int = 50) -> list[RecentItem]` — `RecentItem(device, game_id: str | None, title, system_id, started_at, play_time, as_of: str | None)`; `as_of` = the device's last history ingest time when the device is offline, else None.
  - `games(*, system, q, device: str | None, only_device: bool, favorites: bool, show_unavailable: bool, offset: int, limit: int) -> GamePage` — `GamePage(items: list[GameItem], total: int)`, `GameItem(id, title, system_id, system_name, favorite: bool, availability: dict[str, Availability])`, `Availability(available: bool, reason: Literal["not_on_device", "core_missing"] | None)` for every configured target device.
  - `game(game_id) -> GameItem | None`, `systems() -> list[SystemItem]` (`SystemItem(id, name, manufacturer, game_count)`), `set_favorite(game_id, on: bool) -> bool` (False if unknown game), `last_launched(device) -> LastLaunched | None` (`LastLaunched(game_id, title, at)`), `sync_state(device) -> SyncState | None` (`SyncState(running: bool, status, finished_at, last_good_at, error, seen, added, missing)`).

- [ ] **Step 1: Failing tests.**
  - `tests/test_history.py`: ingest maps entries with known paths to game ids and keeps unresolved ones (`game_id None`) with `media_name`; NFC-card entry without `mediaId` still listed; `recent()` merges both devices newest first; after `fake.refuse = True` and the target going offline, `recent()` still returns that device's entries with `as_of` set; `resolve_now_playing` after `fake.play(id)` gives the game id; an unindexed path gives `game_id None` and `title == mediaName`.
  - `tests/test_queries.py` (synced `sim`): `games(device="office", only_device=True)` excludes *Family Only Quest*; *Gunstar Heroes* on office → `Availability(False, "core_missing")`; on family-room → `(True, None)`; *Family Only Quest* availability for office → `"not_on_device"`; `systems()` lists only systems with present games; `favorites=True` filter; `set_favorite` on an unknown id → False; `last_launched` reads the newest `ok` launch.
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: history, now playing and catalog queries`

---

### Task 10: `launch.py`

Spec: §7.3 (whole section), §12.

**Files:** Create `src/cartridge/launch.py`; Test `tests/test_launch.py`

**Interfaces:**
- Consumes: `Catalog`, `DeviceRegistry`, `Database`, `ApiError`, `LaunchError` subclasses, `refusal`.
- Produces: `LaunchRequest(device: str, game: str, replace: bool, request_id: str)`; `class LaunchService(catalog: Catalog, registry: DeviceRegistry, db: Database, *, clock=time.monotonic, now=utcnow)`:
  - `async launch(req: LaunchRequest) -> dict[str, object]` → `{"device", "game", "started": True}` or raises `ApiError`.
  - `async return_to_menu(device: str) -> dict[str, object]` → `{"device", "stopped": True}` (checks 2, 6, 7, 9, then `target.return_to_menu()`).
  - `IDEMPOTENCY_TTL = 60.0`.
- Checks 2–10 in order exactly as §7.3's table, with the codes/statuses there (check 1 is the web layer's). Check 7 uses `registry.actions.take(device)`; refusal → `ApiError(429, "rate_limited", refusal(...), retry_after=ceil(wait))`. Check 8: a `request_id` seen within 60 s returns the first result (awaiting it if still in flight) and refunds this request's token. Check 9: `registry.lock(device).locked()` → 409 `busy_launching` (refund). Check 10: `status.now_playing` and not `replace` → 409 `playing` with `extra={"now_playing": {"title", "system_id", "game"}}` (refund).
- Then, under the lock: `target.launch(ref, replace=req.replace)`; on `StaleRef` → `catalog.sync(device)` once, re-read `device_media` by `game_id`; not present → 410 `gone` ("the file is gone from this device; sync again"); else retry launch once (a second `StaleRef` → 410 `gone`). Map `LaunchError` subclasses to `ApiError(cls.http_status, cls.code, sentence, extra={"reason": …} for LaunchFailed)`. Write `db.record_launch(device, game, requested_at, outcome=code or "ok", detail)` for every outcome after validation; publish `{"type": "device", "device"}` on success.

- [ ] **Step 1: Failing tests** (`tests/test_launch.py`, synced `sim`; helper `calls(fake)` snapshots `len(fake.received)` before each request):
  - `test_rejections_reach_no_device` — parametrized over: unknown device (404 `unknown_device`); bad game id `"../x"` and unknown valid id (404 `unknown_game`); *Family Only Quest* on office (409 `not_on_device`); *Gunstar Heroes* on office (409 `core_missing`); office offline (`refuse` + wait for status) (503 `offline`); fifth launch inside 15 s (429 `rate_limited`, `retry_after` set); lock held (409 `busy_launching`); playing without replace (409 `playing`, `extra.now_playing.title`). Each asserts zero new fake calls (§13).
  - `test_success_launches_and_logs` (200 body; fake `active`; `launches` row `ok`; `last_launched` updated).
  - `test_replace_flow` (playing → `replace=True` → fake methods include `stop` before `run`).
  - `test_double_tap_is_idempotent` (two concurrent requests, same `request_id` → one `run` on the fake, identical results, one token net).
  - `test_rate_limit_shared_with_menu` (3 launches + 1 menu → 5th action 429).
  - `test_reindex_triggers_one_resync` (`reindex()` then launch → fake sees exactly one `systems` call after the launch began, then `run`; 200).
  - `test_gone_after_resync` (remove the item from fake and reindex → 410 `gone`, one resync).
  - `test_run_errors_map` parametrized: `busy→409 device_busy`, `blocked→409 blocked`, `launch_repair→502 launch_failed` + `extra.reason`, `media_not_found→410 gone`, slow run with patched `RUN_TIMEOUT` → 504 `timeout`; each writes a `launches` row with that outcome.
  - `test_device_drops_during_run` (Review Focus 2): `close_after = "run"` → 503 `offline`, `launches` row `offline`.
  - `test_menu_flow` (200; fake `stop`), `test_menu_unknown_device` (404, no calls).
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: launch and return-to-menu flow`

---

### Task 11: `art.py`

Spec: §8.

**Files:** Create `src/cartridge/art.py`; Test `tests/test_art.py`

**Interfaces:**
- Produces: `PLAYLISTS: dict[str, str]` (Zaparoo system id → libretro playlist; at least `SNES: "Nintendo - Super Nintendo Entertainment System"`, `Genesis: "Sega - Mega Drive - Genesis"`, `NES: "Nintendo - Nintendo Entertainment System"`, `GBA: "Nintendo - Game Boy Advance"`, `Gameboy: "Nintendo - Game Boy"`, `GameboyColor: "Nintendo - Game Boy Color"`, `MasterSystem: "Sega - Master System - Mark III"`, `GameGear: "Sega - Game Gear"`, `TurboGrafx16: "NEC - PC Engine - TurboGrafx 16"`, `PSX: "Sony - PlayStation"`, `Nintendo64: "Nintendo - Nintendo 64"`, `Atari2600: "Atari - 2600"`, `NeoGeo: "SNK - Neo Geo"`); `SYSTEM_COLORS: dict[str, str]` (hex, plus a default); `libretro_url(system_id: str, rel_path: str) -> str | None` (`# ASSUMPTION L-THUMB-URL`: replace each of ``&*/:`<>?\|"`` with `_`, `urllib.parse.quote` path segments); `placeholder_svg(title: str, system_id: str) -> bytes` (title XML-escaped, wrapped, on the system color, 3:4 viewBox, text contrast ≥ 4.5:1).
- `ArtResult(body: bytes, content_type: str, etag: str, found: bool)`; `class ArtCache(db, registry, catalog, art_dir: Path, *, http: httpx.AsyncClient, online: bool, version: str, now=utcnow, clock=time.monotonic)`: `async get(game_id: str) -> ArtResult | None` (None for an unknown game), `async prefetch(game_ids: Iterable[str]) -> None` (concurrency 2), `async close()`.
- Chain per §8 table. Cache file `art_dir/<id[:2]>/<id>.<webp|png|jpg>`, written atomically (tmp + rename). Single-flight per game id. libretro: shared `TokenBucket(capacity=1, rate=1)` via `acquire`, `User-Agent: cartridge/<version>`; 404 → miss; 429 → pause libretro until now + `Retry-After` seconds (default 60) and don't record a miss; network error → no miss recorded. Miss (`status none`) only when every applicable source answered "no art"; `retry_after = now + 7 days`, during which the placeholder is served without any fetch. `online=False` skips libretro. ETag = first 16 hex chars of sha256(body), quoted.

- [ ] **Step 1: Failing tests** (`tests/test_art.py`, synced `sim`, `httpx.MockTransport` recording requests):
  - `test_zaparoo_image_first` (*Super Metroid* has `image: true` → found, `image/png`, no HTTP request, file cached; second `get` reads the cache with no `media.image` call).
  - `test_libretro_second` (*Donkey Kong Country*: transport returns PNG for the expected URL `https://thumbnails.libretro.com/Nintendo%20-%20Super%20Nintendo%20Entertainment%20System/Named_Boxarts/Donkey%20Kong%20Country%20%28USA%29.png`, UA `cartridge/0.1.0`).
  - `test_libretro_name_replacement` (`Façade & Friends - Part II (USA).sfc` → `&` becomes `_`).
  - `test_miss_is_cached_for_7_days` (404 → placeholder `found False`; second `get` makes no request; after advancing `now` 8 days, it asks again).
  - `test_retry_after_honored` (429 `Retry-After: 120` → placeholder, no miss row; a `get` within 120 s skips libretro; after, it asks).
  - `test_online_off_skips_libretro`, `test_unknown_system_skips_libretro`.
  - `test_placeholder_escapes_title` (`<script>` title → escaped in SVG).
  - `test_no_real_network` (the transport raises on any host other than `thumbnails.libretro.com`).
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: artwork chain and cache`

---

### Task 12: `access.py`

Spec: §9.4, §11. Derive `AccessPolicy`, `AccessVerifier`, `AccessDenied` from `../mcp-server-shared/src/mcp_shared/remote.py`; header: "Local copy pending the access.py split in mcp-server-shared (spec §11)". Drop CORS/OPTIONS handling and the forwarded-header logic; email error mentions `REMOTE_ALLOWED_EMAILS`.

**Files:** Create `src/cartridge/access.py`; Test `tests/test_access.py`

**Interfaces:**
- Produces: `ACCESS_HEADER = "cf-access-jwt-assertion"`, `HEALTH_PATH = "/healthz"`; `AccessPolicy(team_domain, audiences, emails)`; `AccessVerifier(policy, key_resolver: KeyResolver | None = None)` with `async verify(token) -> dict[str, Any]`; `AccessMiddleware(app, verifier: AccessVerifier, *, health_check: Callable[[], Awaitable[bool]])` — `/healthz` (GET/HEAD) answered first with `{"status": "ok"}` 200 or `{"status": "not_ready"}` 503 and nothing else; every other http request needs a valid assertion or gets 403 `{"code": "forbidden", "message": "Sign in through Cloudflare Access to view this."}`; `policy_from_settings(settings: Settings) -> AccessPolicy`.

- [ ] **Step 1: Failing tests** (RSA key from `cryptography`, tokens via `jwt.encode`, `key_resolver` returns the public key; tiny Starlette app behind the middleware): no JWT → 403; bad AUD → 403; wrong issuer → 403; expired (beyond 30 s leeway) → 403; bad signature → 403; email not in allowed set → 403; valid → 200; `/healthz` → 200 without JWT and body exactly `{"status": "ok"}`; health_check False → 503 `{"status": "not_ready"}`; the 403 body has no CORS header.
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: Cloudflare Access verification`

---

### Task 13: Web — common DTOs, read routes, LAN app and guards

Spec: §7.3 check 1, §9.1, §9.5, §9.6, §9.8, §10 data needs, §12.

**Files:** Create `src/cartridge/web/{__init__,common,lan}.py`, `src/cartridge/core.py` (only the `Core` dataclass here; Task 15 adds lifecycle); Test `tests/test_web_lan.py`; Modify `tests/conftest.py` (fixture `core`: synced `Core` over `sim`; fixture `lan`: `httpx.AsyncClient(transport=ASGITransport(build_lan_app(core, hosts)), base_url="http://cartridge.local")`)

**Interfaces:**
- `core.py`: `@dataclass class Core: settings: Settings; db: Database; registry: DeviceRegistry; catalog: Catalog; art: ArtCache; hub: EventHub; launcher: LaunchService`.
- `web/common.py`:
  - DTO `TypedDict`s (no path/ref/host fields, ever): `ErrorDTO{code, message}`, `NowPlayingDTO{title, system_id, game: str | None, started_at: str | None}`, `LastLaunchedDTO{game, title, at}`, `SyncDTO{running, status, finished_at, last_good_at, error, seen, added, missing}`, `DeviceDTO{name, label, model, platform, state, message, version, now_playing: NowPlayingDTO | None, last_launched: LastLaunchedDTO | None, sync: SyncDTO | None}`, `SystemDTO{id, name, manufacturer, game_count}`, `AvailabilityDTO{available, reason}`, `GameDTO{id, title, system_id, system_name, favorite, devices: dict[str, AvailabilityDTO]}`, `GamePageDTO{games, total, offset}`, `RecentDTO{device, game, title, system_id, started_at, play_time, as_of}`, `ConfigDTO{mode: Literal["lan","remote"], version, devices_configured: int, synced: bool}`.
  - `device_dto(core, name) -> DeviceDTO`, `game_dto(item) -> GameDTO`, etc.; `error_response(err: ApiError) -> JSONResponse` (sets `Retry-After` when present); `SECURITY_HEADERS` (`Content-Security-Policy: default-src 'self'; img-src 'self' data:; connect-src 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`) via `SecurityHeadersMiddleware`.
  - `read_routes(core: Core, mode: Literal["lan","remote"]) -> list[BaseRoute]`: `GET /` (index.html), `Mount /static` (`StaticFiles(packages=[("cartridge","static")])`), `GET /healthz` (LAN: `{"status":"ok"}` / 503 `{"status":"not_ready"}` via `db.ping()`), `GET /api/config`, `GET /api/devices`, `GET /api/systems`, `GET /api/games` (query `system`, `q`, `device`, `only_device` (default `1` when `device` is set), `favorites`, `show_unavailable`, `offset` ≥ 0, `limit` 1–500 default 120; unknown `device` → 404 `unknown_device`; bad ints → 400 `bad_request`), `GET /api/games/{id}` (bad/unknown id → 404 `unknown_game`), `GET /api/recent`, `GET /api/events` (SSE: `event: <type>\ndata: <json>\n\n`, `: ping` every 15 s, subscribes to the app's own pool; `TooManyClients` → 503 `too_many_clients`; first event is `hello` with the device list), `GET /art/{id}` (validated id only; `ETag`, `If-None-Match` → 304; `Cache-Control` per Decisions).
- `web/lan.py`: `default_lan_hosts(bind_host: str) -> frozenset[str]` (Decisions); `class LanGuard` ASGI middleware (order: forwarded headers `forwarded, x-forwarded-for, x-forwarded-host, x-forwarded-proto, x-real-ip, true-client-ip, cf-connecting-ip, cf-ray, via` → 403; Host not in allow-list → 403; for methods other than GET/HEAD: `Content-Type` media type `application/json`, `X-Cartridge: 1`, `Origin` absent or `http(s)://<allowed host>[:port]` → else 403; all 403s are `{"code": "bad_request", "message": <sentence>}`); `build_lan_app(core: Core, hosts: frozenset[str]) -> Starlette` = read routes + write routes: `POST /api/launch` (body schema: exactly keys `device` str, `game` str, `replace` bool, `request_id` UUID string → else 400 `bad_request`), `POST /api/devices/{name}/menu`, `POST /api/devices/{name}/sync` (202 `{device, started: true}`; `SyncRateLimited` → 429 `rate_limited` + `Retry-After`; unknown → 404), `POST /api/favorites/{id}` and `DELETE /api/favorites/{id}` (unknown → 404 `unknown_game`; 200 `{game, favorite}`).
- No route or middleware ever sets `Access-Control-*`.

- [ ] **Step 1: Failing tests** (`tests/test_web_lan.py`, all write requests send `Content-Type: application/json` + `X-Cartridge: 1` unless the test drops one):
  - Guards (§13): each forwarded header → 403; `Host: evil.example.com` → 403; POST without `X-Cartridge` → 403; with `Origin: http://evil.example.com` → 403; with `Content-Type: application/x-www-form-urlencoded` → 403; `Origin` matching an allowed host → passes.
  - `test_host_variants_allowed` (Review Focus 4): hosts `{"cartridge.local", "192.0.2.5", "::1"}`; `Host: 192.0.2.5:8770`, `Host: CARTRIDGE.LOCAL`, `Host: [::1]:8770` → 200 on `/api/devices`.
  - `test_default_lan_hosts`: wildcard bind includes `localhost` and `socket.gethostname().lower()`; `"192.0.2.9"` bind includes `"192.0.2.9"`.
  - `test_launch_body_schema` (Review Focus 3): missing `device`, extra key, `replace: "yes"`, bad `request_id`, non-JSON body → 400 `bad_request`, fake calls unchanged.
  - `test_launch_end_to_end` (200) and `test_launch_playing_conflict_payload` (409 `playing` with `now_playing.title`).
  - `test_no_cors_headers_anywhere` (GET, POST, `OPTIONS /api/launch`) and `test_security_headers` (CSP exact string).
  - `test_games_api` (paging `total`/`offset`, `device=office` hides *Family Only Quest*, availability reasons, unknown device 404, `limit=0` 400) and `test_search_api_survives_syntax` (Review Focus 1: `q` values from Task 3 → 200).
  - `test_favorites_toggle`, `test_sync_now_202_then_429`, `test_menu_route`.
  - `test_art_route` (200 + `ETag`; `If-None-Match` → 304; `/art/..%2f..%2fetc` → 404).
  - `test_sse_stream` (first event `hello`; a `fake.play()` produces a `device` event) and `test_sse_client_limit` (Review Focus 5: hub `max_clients=2` → third 503 `too_many_clients`; after one closes, a new one connects).
  - `test_healthz_not_ready_when_db_closed`.
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: LAN web app, DTOs and guards`

---

### Task 14: Remote app and the leak test

Spec: §4 "One process, two ASGI apps", §9.1, §9.4, §13 Remote app + Leak test.

**Files:** Create `src/cartridge/web/remote.py`; Test `tests/test_web_remote.py`, `tests/test_leaks.py`

**Interfaces:**
- Consumes: `read_routes`, `AccessMiddleware`, `AccessVerifier`, `policy_from_settings`.
- Produces: `build_remote_app(core: Core, verifier: AccessVerifier) -> Starlette` = `read_routes(core, "remote")` only, wrapped in `AccessMiddleware` and `SecurityHeadersMiddleware`. `LAN_WRITE_ROUTES: list[tuple[str, str]]` exported from `web/lan.py` (method, path template) so tests can iterate it.

- [ ] **Step 1: Failing tests.**
  - `tests/test_web_remote.py` (signed tokens as in Task 12): `test_every_write_route_404s` (iterate `LAN_WRITE_ROUTES` with a valid JWT → 404 each; and the fake recorded no calls); no JWT → 403; bad AUD → 403; expired → 403; valid → 200 on `/api/games`; `/healthz` without JWT → 200 `{"status":"ok"}`; `/api/config` → `mode == "remote"`; forwarded headers are fine here (it sits behind cloudflared).
  - `tests/test_leaks.py` (§9.1, both apps, synced `sim`, after a launch so now-playing/last-launched/history are populated): GET every JSON route (`/api/config`, `/api/devices`, `/api/systems`, `/api/games?show_unavailable=1&limit=500`, `/api/games?device=office`, each `/api/games/{id}`, `/api/recent`), plus every LAN write response and error responses (`unknown_game`, `playing`, `gone`), plus the first 3 SSE events; walk every string in each JSON body and fail if it contains any fixture `abs_path`, any `rel_path` (NFC and NFD forms), `"/media/"`, `"127.0.0.1"`, or a configured host.
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: read-only remote app; leak test`

---

### Task 15: `core.py` lifecycle and `cli.py`

Spec: §4, §5 `cli.py`, §7.1 "When it runs", §7.2, §8 prefetch, §9.4 startup refusal, §12, §14 settings.

**Files:** Modify `src/cartridge/core.py`; Create `src/cartridge/cli.py`; Test `tests/test_core.py`, `tests/test_cli.py`

**Interfaces:**
- `core.py`: `async build_core(settings: Settings, devices: Devices, *, http: httpx.AsyncClient | None = None, factory: TargetFactory = ZaparooTarget) -> Core`; `class Background(core)` with `start()`/`stop()` running: on a device's first `online` status → `catalog.sync` then `ingest_history`; every later `online` → `ingest_history`; `media.stopped` → `ingest_history`; `indexing.finished` → `catalog.sync`; every `sync_interval_hours` → sync each online device; after a sync → `art.prefetch` of that device's games; every `TargetEvent` → `hub.publish({"type": "device", "device": name})`. All tasks via `spawn_task`, stopped with `stop_tasks`. `async close_core(core)`.
- `cli.py`: `main(argv: list[str] | None = None) -> int`; subcommands `serve` (default), `check`, `sync [--device NAME]`, `simulate [--dir DIR] [--fakes-only] [--lan-bind HOST:PORT]`.
  - `serve`: `logsafe.setup_logging`; `load_settings(os.environ)` and `load_devices` (`ConfigError` → print the sentence to stderr, exit 2: this is the "refuses to start" path); build core; two `uvicorn.Server(uvicorn.Config(app, host, port, lifespan="off", log_config=None, access_log=False))` served with `asyncio.gather`; remote only when `remote_enabled`; SIGTERM/SIGINT → graceful shutdown (`hub.close()` first so SSE streams end).
  - `check`: prints each config problem, then each device's status after waiting up to 5 s for `tried`, then `db.ping()`; exit 0 only if no problems, all devices online and DB OK; database errors say "delete or restore <path>; Cartridge rebuilds it on the next sync".
  - `sync`: syncs each (or one) device, prints `SyncResult`s, exit 1 if any failed.
  - `simulate`: starts `Simulation` with both fixtures under `--dir` (default `./sim-data`), writes `devices.yaml` there; with `--fakes-only` prints its path and waits; otherwise sets `REMOTE_ENABLED=0` unless set, `DATA_DIR=<dir>/data`, `DEVICES_FILE=<dir>/devices.yaml`, `LAN_BIND` from the flag (default `127.0.0.1:8770`) and runs `serve` in-process. Prints `Cartridge simulator: http://<lan-bind>/`.

- [ ] **Step 1: Failing tests.**
  - `tests/test_core.py` (against `sim`, small `sync_interval_hours`): startup sync happens once each device is online; `fake.finish_indexing()` triggers a sync (`sync_runs` count +1); `fake.quit_game()` triggers history ingest; a status change publishes a `device` event; `close_core` leaves no running tasks.
  - `tests/test_cli.py` (subprocess `python -m cartridge.cli` where it must be a process; in-process `main([...])` otherwise): `serve` with `REMOTE_ENABLED=1` and no Access settings → exit 2, stderr names `CF_ACCESS_TEAM_DOMAIN`; `check` against a written sim `devices.yaml` with one bad entry → exit 1 and prints the bad entry's sentence and both devices online; `sync --device office` → exit 0; `simulate` subprocess on a free port answers `GET /api/games` with games within 10 s, then terminates cleanly on SIGTERM.
- [ ] **Step 2–4:** FAIL → implement → PASS; gate.
- [ ] **Step 5: Commit** `feat: core lifecycle and CLI`

---

### Task 16: Frontend (shell, selector, grid, sheets, live updates)

Spec: §10 (whole), §9.8, §1 success criteria 1–3.

**Files:** Create `src/cartridge/static/{index.html,app.js,api.js,grid.js,devices.js,focus.js,style.css}`, `src/cartridge/static/fonts/{InterVariable.woff2,OFL.txt}` (download once from the rsms/inter v4.1 GitHub release; commit them); Test `tests/e2e/test_ui.py`, `tests/e2e/conftest.py`; Modify `.github/workflows/ci.yml` (job `e2e`)

**Interfaces:**
- Consumes the HTTP API of Tasks 13–14 only.
- `api.js`: `getJSON(path)`, `postJSON(path, body)` (always `Content-Type: application/json` + `X-Cartridge: 1`), `del(path)`, `events(onEvent)` (EventSource, reconnect with backoff 1 s → 30 s), throws `ApiFailure{status, code, message, body}`.
- `devices.js`: selector pill (label, model, status dot with text state — never color alone), sheet listing each device (status sentence, now playing, last launched, sync state/error + last good time, **Return to menu** with confirm, **Sync now**); selection persisted under `localStorage["cartridge.device"]` inside `try/catch` (session-only fallback); auto-select only when exactly one device; stale saved name ignored.
- `grid.js`: 3:4 frames with `object-fit: contain`, `loading="lazy"` images from `/art/<id>`, title below; chips: one per system from `/api/systems` + Favorites + Recently played + A–Z; "Only games for {label}" toggle (on by default); dimmed unavailable tiles with the reason text; infinite scroll via `IntersectionObserver` on a sentinel; empty states: never synced (explains indexing in Zaparoo's UI, README link, Sync now) and no matches.
- Game sheet: art, title, system, which devices have it, star toggle, **Play on {label}** (disabled with reason: no device selected / offline / not on device / core missing); remote mode (`/api/config.mode == "remote"`) shows "Browse only: launch from home" and no Play, no menu/sync/star controls. Launch posts with a fresh `crypto.randomUUID()` per tap (reused on the confirm resend); 409 `playing` → confirm sheet with exactly "*{title}* is running on {label}. Replace it? Unsaved progress will be lost." → resend with `replace: true`; other errors show `message`.
- `focus.js`: TV mode (`?tv=1` or toggle, persisted like the device) adds `.tv` on `<body>`: larger tiles, visible focus ring, spatial arrow navigation (nearest focusable in the arrow's direction by rect centers), Enter activates, Escape/Backspace close the top sheet; every control is a real `<button>`/`<input>` so it's reachable without a pointer.
- `style.css`: tokens on `:root`, dark arcade palette, e.g. `--bg #0d0f1a`, `--surface #171a2b`, `--surface-2 #22263d`, `--text #eef0ff`, `--muted #a9aecb`, `--accent #ffcc33`, `--accent-2 #4de1ff`, `--danger #ff5c7a`, `--ok #5cf2a4`, all text pairs ≥ 4.5:1; `@font-face` Inter from `/static/fonts/InterVariable.woff2`; grid `repeat(auto-fill, minmax(112px, 1fr))` so 412 px gives ≥ 3 columns; no horizontal overflow. No inline `<script>`/`<style>`/`style=` in HTML (CSP).
- e2e harness (`tests/e2e/conftest.py`): session fixture starts `python -m cartridge.cli simulate --dir <tmp> --lan-bind 127.0.0.1:<free port>`, waits for `/healthz`, yields the base URL, terminates. Uses Playwright's sync API (`playwright.sync_api`), Chromium.

- [ ] **Step 1: Failing e2e tests** (`tests/e2e/test_ui.py`, `pytestmark = pytest.mark.e2e`):
  - `test_phone_grid` (412×915): ≥ 3 tiles in the first row (compare `boundingBox().y`), `document.documentElement.scrollWidth <= 412`.
  - `test_pick_device_then_launch` (phone): two devices → Play disabled with "Choose a device"; pick Family Room → selector shows "Family Room"; open *Super Metroid* → Play → toast/sheet confirms started; device sheet shows now playing *Super Metroid*.
  - `test_replace_confirm` (phone): with *Super Metroid* running, launch *Gunstar Heroes* → confirm sheet text contains "is running on Family Room. Replace it? Unsaved progress will be lost." → confirm → now playing updates.
  - `test_filter_and_search` (phone): SNES chip limits tiles to SNES; search "facade" finds *Façade & Friends: Part II*.
  - `test_unavailable_reason` (phone): select Office, turn off "Only games for Office", *Gunstar Heroes* tile shows "core not installed".
  - `test_tv_keyboard_only` (1920×1080, `?tv=1`): with only the keyboard, ArrowRight/ArrowDown move focus between tiles, Enter opens the sheet, Escape closes it; no horizontal scroll.
  - `test_storage_blocked` (init script making `localStorage` throw): the app still loads and selecting a device works.
- [ ] **Step 2: Run** `python -m playwright install chromium && python -m pytest -m e2e -v` → FAIL.
- [ ] **Step 3: Implement** the static files.
- [ ] **Step 4: Run** `python -m pytest -m e2e -v` → PASS; gate (incl. `check_js`).
- [ ] **Step 5: CI job `e2e`:** Python 3.12, `pip install -e ".[dev]"`, `python -m playwright install --with-deps chromium`, `python -m pytest -m e2e`.
- [ ] **Step 6: Commit** `feat: browser UI with TV mode; Playwright smoke`

---

### Task 17: Deploy, docs, assumptions registry

Spec: §13 assumptions table, §14 (deployment, README, tunnel), §12 README pointers, D5.

**Files:** Create `deploy/alpine/{install.sh,cartridge.initd,run.sh,conf.d.example,devices.yaml.example,smoke-test.sh}`, `README.md`, `HARDWARE_VALIDATION.md`, `tests/test_assumptions.py`; Modify `.github/workflows/ci.yml` (job `alpine`), `CLAUDE.md` (status line)

**Interfaces:**
- `tests/test_assumptions.py`: `ASSUMPTIONS` = the 11 §13 ids (literal tuple in the test, with the claims); fails when an id is missing from README's assumptions table or HARDWARE_VALIDATION.md, when docs or code cite an unknown id (pattern `\b(?:Z|C|L)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b`), or when an id is cited nowhere in `src/**/*.py` as `# ASSUMPTION <id>`.
- `install.sh` (idempotent, `$1` = source dir): `apk add python3 py3-pip nodejs`-free install (node not needed at runtime); user/group `cartridge`; venv `/opt/cartridge/venv` with `pip install <src>`; `/var/lib/cartridge` 0700 owned by `cartridge`; `/etc/conf.d/cartridge` from `conf.d.example` (0640, root:cartridge) and `/etc/cartridge/devices.yaml` from the example only if absent; installs `/etc/init.d/cartridge` and `/opt/cartridge/run.sh`.
- `cartridge.initd`: `supervise-daemon`, `command=/opt/cartridge/run.sh`, `command_user=cartridge:cartridge`, `need net`, output to `/var/log/cartridge/cartridge.log`.
- `run.sh`: exports the conf.d settings and `exec /opt/cartridge/venv/bin/cartridge serve`.
- `smoke-test.sh` (python:3.12-alpine, shaped like misterfpga's): install; pytest on musl; `cartridge simulate --fakes-only --dir /var/lib/cartridge/sim` as the service user; copy its `devices.yaml`; conf.d with `REMOTE_ENABLED=1`, `CF_ACCESS_TEAM_DOMAIN=example.cloudflareaccess.com`, `CF_ACCESS_AUD=smoke`, `LAN_BIND=127.0.0.1:8770`; start the service; curl LAN `/healthz` (200) and `/api/games` (200, non-empty), remote `/healthz` (200) and `/api/games` (403); check the process user and file modes; stop.
- README sections, in order (§14): what it is; screenshots from the simulator (`docs/screenshots/{phone,tv}.png`, taken with Playwright from `simulate`); **Install Zaparoo Core on a MiSTer** (the four steps verbatim from §14); device config (`devices.yaml` example, validation, `strip_prefixes`); deploy on Alpine; cloudflared and Access (ingress `games.example.com → http://<ct>:8771`, Access app, `CF_ACCESS_*`, `REMOTE_ALLOWED_EMAILS`; **the LAN port is never a tunnel target**); settings table (§14); artwork sources and their terms (§8; `ART_ONLINE=0`); Home Assistant tracking via `mcp-server-misterfpga track` (D5: Cartridge publishes nothing); troubleshooting per §12 rows (incl. `encryption = false` for "needs pairing"); assumptions table `| Id | Claim | Status |` with every id `unverified`; `cartridge simulate`.
- HARDWARE_VALIDATION.md: one checklist section per id with the steps to verify on a real MiSTer and the expected result.

- [ ] **Step 1: Failing test** `tests/test_assumptions.py` → FAIL (no docs).
- [ ] **Step 2: Write** README, HARDWARE_VALIDATION.md, deploy files; take screenshots with a short Playwright script against `simulate` (not committed as a test).
- [ ] **Step 3: Run** `python -m pytest tests/test_assumptions.py -v` → PASS; gate.
- [ ] **Step 4: Run** the smoke test locally: `docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh` → exit 0.
- [ ] **Step 5: CI job `alpine`** (as misterfpga's). Update CLAUDE.md status ("verified against the simulator only; HARDWARE_VALIDATION.md is the checklist").
- [ ] **Step 6: Commit** `docs: README, hardware validation, Alpine deploy and smoke test`
