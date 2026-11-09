# Cartridge: design

Status: approved in brainstorming on 2026-11-07; §8 (artwork) amended 2026-11-08. This is
the source of truth for the implementation plan. A decision here changes only by editing this file in a reviewed PR.

Cartridge is a self-hosted web app. It shows a game library as a box-art grid that you can
filter by system and search. Picking a game launches it on a device you choose explicitly.
v1 drives MiSTer FPGA devices through Zaparoo Core. The launch backend is a pluggable
`LaunchTarget`, so a Batocera machine can be added later without reshaping the app.

---

## 1. Intent and success criteria

**Who uses it**
- Anyone on the home LAN, with no login: they browse, launch, return to menu, and star
  favorites.
- People the owner allows through Cloudflare Access, from outside the house: they browse
  only. They can never launch or control a device.

**Success looks like**
1. From a phone on the LAN, you can find a game by system or search and launch it on a
   chosen MiSTer in about three taps. The device you are launching on is always visible.
2. The same UI works on a TV browser with a D-pad or arrow keys alone.
3. Each device card shows whether it is online and what it is playing. A launch onto a busy
   device asks before replacing the running game.
4. From outside the house, behind Access, you see the same catalog read-only.
5. Nothing in the browser ever sees a ROM filesystem path, and no request can choose a device
   that isn't in config.
6. Strict typing, linting and tests run in CI. Everything is built and tested against
   fixtures and a simulated Zaparoo, with no hardware.

**Generic by design.** No personal names, hostnames or domains appear in code, docs, examples
or tests. Examples use `example.com` and device names like `family-room` and `office`.

---

## 2. Decisions (brainstorming, 2026-11-06 → 07)

| # | Question | Decision |
|---|---|---|
| D1 | Who launches | Anyone on the LAN, no login. Remote access is read-only |
| D2 | Remote view audience | Behind Cloudflare Access (allowed emails), and the app verifies the Access JWT itself |
| D3 | Launch onto a busy device | Confirm first: the sheet names the running game and warns that unsaved progress will be lost. A separate **Return to menu** also confirms |
| D4 | Favorites / recent / last launched | All three. Favorites are household-wide, in SQLite. Recently played comes from each device's own Zaparoo `media.history`. Each device card shows its last launch |
| D5 | MQTT / Home Assistant | None from Cartridge. Play tracking is `mcp-server-misterfpga track` (and Batocera's playtracker later). The README says so |
| D6 | Name / host | **Cartridge**. Repo `SDNick484/cartridge`; deployed at `games.<owner domain>` |
| D7 | Catalog source | Each device's Zaparoo index, read through the API. The app does not scan the share |
| D8 | Artwork (amended 2026-11-08) | **The scraped share is the source of truth.** The owner scrapes the ROM share once, outside Cartridge, with a hash-matching scraper. Cartridge reads that art through its optional read-only share reader and through Zaparoo `media.image`, then falls back to libretro-thumbnails, then to a generated tile. Fallback art is upgraded when share art appears. Everything is cached locally. Cartridge holds no scraper credentials |
| D9 | Stack | Async Python (Starlette + uvicorn), SQLite, plain-JS SPA, SSE. Two listeners in one process |
| D10 | Shared code | Promote the Zaparoo client (and Access verification) into `mcp-server-shared`. Cartridge starts with local copies until those PRs merge |
| D11 | Batocera later | `LaunchTarget` Protocol now. The cabinet most likely uses the same `ZaparooTarget` with `platform: batocera`; an `EmulationStationTarget` is the fallback |

---

## 3. Zaparoo facts this design depends on

These come from the Zaparoo Core API docs, read 2026-11-06, and from
`mcp-server-misterfpga`, which verified them against Core's source. Anything not yet
confirmed on hardware is an `Assumption` (§13).

- **Transport.** JSON-RPC 2.0 over WebSocket at `ws://<host>:7497/api/v0.1`. String ids;
  the text `ping` gets the reply `pong`. There is also an unauthenticated HTTP `/health`
  that returns `starting|ready|failed`.
- **Launch is `run`.** `launch` is a deprecated alias with identical params; Cartridge only
  ever sends `run`.
  - `run` takes ZapScript `text` (≤ 8192 bytes) and never sets `unsafe`.
  - It waits for the launch to finish (a 30 s server timeout) and returns `null`, or an
    error whose `data.category` is one of `media_not_found`, `blocked`, `timeout`,
    `launch_repair` (with `data.reason`) or `busy`.
- **No path or mediaId launch.** The ZapScript for a game comes from
  `media.meta {mediaId}` → `media.zapScript`, which Core builds itself. Cartridge never
  builds ZapScript from a path.
- **`mediaId` is local.** It is a row id in one device's database and changes after a
  reindex. It is never a Cartridge game id.
- **Catalog and availability.**
  - `media.search`: cursor-paginated (`maxResults` ≤ 1000, `pagination.nextCursor`),
    filterable by `systems`.
  - `systems {all: true}`: includes systems whose launcher (core) is unavailable, with
    `mediaCount`.
  - `launchers`.
- **`media.image`** takes `mediaId` (or `system` + `path`), `imageTypes`, `maxSize`, and
  `delivery: inline`. It returns base64 `data` and a `contentType` (usually WebP).
- **Built-in scrapers** are only `gamelist.xml`, `media-folder` and `mister-docs`. Core has
  no online artwork source, so on a fresh share `media.image` often has nothing to give.
  A share scraped into ES-style `gamelist.xml` plus image folders is exactly what those
  scrapers read, which is why §8 makes the scraped share the primary art source.
- **History, active media and stop.**
  - `media.history {limit ≤ 100, distinctMedia}` returns entries with `systemId`,
    `mediaPath`, `mediaName`, `startedAt`, `playTime` and an optional `mediaId`.
  - `media.active` returns the active media or `null`.
  - `stop` returns a MiSTer to its menu.
  - The notifications `media.started`, `media.stopped` and `media.indexing` are pushed.
- **Access tiers.**
  - Localhost has full access.
  - Remote clients need pairing (PAKE, P-256, a 6-digit PIN on the device, then
    AES-256-GCM sessions) when `encryption = true`.
  - Unpaired "legacy" plaintext clients are still admitted on MiSTer and Batocera (also
    MiSTeX, LibreELEC and ReplayOS), and MiSTer sets no encryption default. v1 uses legacy
    access.
  - A configured static API key makes Core refuse every key-less client, the Zaparoo phone
    app included, so Cartridge never uses one.
  - An "encryption required" rejection is the JSON-RPC error `-32002`.

---

## 4. Architecture

```
                 LAN (phones, TV browsers)                    Internet
                          │                                       │
                          ▼                                       ▼
   ┌───────────── cartridge (one process, CT on Proxmox) ────────────────────┐
   │  LAN app  :8770 (bind configurable)      Remote app :8771 (cloudflared)  │
   │  ├ browse + favorites + launch/menu      ├ browse only (no write routes) │
   │  └ Host/Origin/CSRF guards, refuses      └ Access JWT required on every  │
   │    proxied requests                        request (fails closed)        │
   │                    \                      /                              │
   │                     ▼                    ▼                               │
   │   core: Catalog (SQLite) · ArtCache · DeviceRegistry · EventHub (SSE)    │
   │                             │                                            │
   │                 LaunchTarget (Protocol)                                  │
   │                 └ ZaparooTarget × N  (one WebSocket each)                │
   └─────────────────────────────┼────────────────────────────────────────────┘
                                 ▼
                 Zaparoo Core on each MiSTer (ws :7497)
                 ROMs on a shared SMB/CIFS mount, indexed by each Core
```

**One process, two ASGI apps.** Both apps share one core: database, targets, art cache
and event hub. uvicorn serves them on two sockets from a single event loop.
- The **remote app is built from a route table that has no write routes at all**.
  Read-only remotely is a structural fact, not a header check.
- A test asserts that every write path returns 404 on the remote app.

**Why async, not Matinee's Flask.**
- Each device needs a long-lived WebSocket with push notifications: now-playing, online
  status, indexing.
- Every browser needs a live SSE stream.
- Bridging a background asyncio loop into sync Flask workers is the fragile part. A single
  async process avoids it.
- Matinee's conventions still apply: SQLite, a plain-JS SPA with no build step, a phone-first
  grid with at least 3 columns at 412 px, OpenRC on Alpine, and settings in
  `/etc/conf.d/cartridge`.

---

## 5. Components (`src/cartridge/`)

Each unit has one job and is testable alone.

| Module | Responsibility | Depends on |
|---|---|---|
| `config.py` | Load `/etc/conf.d/cartridge` env + `devices.yaml`; validate; problems as sentences. Frozen dataclasses | PyYAML |
| `ids.py` | Game id derivation and validation (§6.2) | stdlib |
| `db.py` | SQLite schema, migrations (`PRAGMA user_version`), typed query functions; WAL; FTS5 | sqlite3 |
| `targets/base.py` | `LaunchTarget` Protocol, `DeviceStatus`, `CatalogEntry`, `HistoryEntry`, `TargetRef`, `LaunchError` hierarchy | — |
| `targets/zaparoo.py` | `ZaparooTarget`: implements the Protocol over `zaparoo_client` | `zaparoo_client` |
| `zaparoo_client.py` | WebSocket JSON-RPC client: ids, reconnect/backoff, ping, notifications, **`ALLOWED_METHODS`**, transport seam (`legacy` now, `paired` later). Local copy pending the shared promotion (§11) | aiohttp |
| `registry.py` | `DeviceRegistry`: config → targets; lookup by name only; status snapshot; per-device locks and rate limits | targets, `ratelimit` |
| `catalog.py` | Sync each device's index into SQLite; availability; history ingest; queries for the API | db, registry |
| `art.py` | Artwork chain and ranks, local cache, upgrades after sync, versioned art URLs, placeholder tiles, fetch rate limit (§8) | db, registry, gamelist, httpx |
| `gamelist.py` | Read-only share reader: parses ES-style `gamelist.xml` under `ART_SHARE_DIR` and returns contained, size-capped, sniffed image bytes (§8.2) | defusedxml |
| `launch.py` | The launch/return-to-menu flow and its validation order (§7) | catalog, registry |
| `events.py` | `EventHub`: fan-out of device/now-playing/sync events to SSE clients | — |
| `web/lan.py`, `web/remote.py`, `web/common.py` | Starlette apps, route tables, DTOs, guards (§9) | everything above |
| `access.py` | Cloudflare Access JWT verification (ASGI middleware). Local copy pending the shared promotion | pyjwt[crypto] |
| `logsafe.py`, `tasks.py`, `ratelimit.py` | From `mcp-server-shared` via `scripts/sync_shared.py` + `shared.lock`; **never edited here** | — |
| `cli.py` | `serve` (default), `check` (config + reach each device + share mount), `sync`, `art-refresh`, `simulate` | — |
| `sim/` | `FakeZaparoo` (aiohttp server speaking real JSON-RPC), fixtures, `Simulation` harness; used by tests **and** `cartridge simulate` | aiohttp |
| `static/` | `index.html`, ES-module JS (`app.js`, `api.js`, `grid.js`, `devices.js`, `focus.js`), `style.css`, bundled font | — |

### 5.1 `LaunchTarget` Protocol

```python
class LaunchTarget(Protocol):
    name: str                      # config key, e.g. "family-room"
    model: str                     # free text from config, e.g. "MiSTer Pi"
    platform: Literal["mister", "batocera"]

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    def status(self) -> DeviceStatus: ...                 # online, error sentence, version, now_playing
    def subscribe(self, listener: Callable[[TargetEvent], None]) -> None: ...

    async def systems(self) -> list[SystemAvailability]: ...
    def catalog(self) -> AsyncIterator[CatalogEntry]: ...  # system_id, title, rel_path, abs_path, ref
    async def artwork(self, ref: TargetRef, max_size: int) -> Image | None: ...
    async def history(self, limit: int) -> list[HistoryEntry]: ...

    async def launch(self, ref: TargetRef) -> None: ...   # raises LaunchError subclasses
    async def return_to_menu(self) -> None: ...
```

- `TargetRef` is opaque to everything except the target that issued it. For Zaparoo it
  wraps a `mediaId`; an ES target would wrap its own game id.
- `abs_path` and `TargetRef` live only in `device_media` and never leave the server.

---

## 6. Data

### 6.1 Device config (`/etc/cartridge/devices.yaml`, user-editable)

```yaml
devices:
  - name: family-room          # [a-z0-9-]{1,32}; the id used in URLs and the selector
    label: Family Room          # shown in the UI
    model: SuperStation One     # free text, shown in the UI
    platform: mister            # mister | batocera (batocera not implemented in v1)
    transport: zaparoo
    host: 192.0.2.10
    port: 7497                  # default 7497
    auth: legacy                # legacy (v1) | paired (later; key file under state dir)
  - name: office
    label: Office
    model: MiSTer Pi
    platform: mister
    transport: zaparoo
    host: 192.0.2.11
```

- Validation runs at startup: names must be unique and match the pattern, the host must be
  an IP or hostname, and the port 1–65535.
- A bad device entry is reported by `check` and on the device card ("misconfigured: …").
  It doesn't stop the other devices from working.
- The device list comes only from this file, never from a request.

### 6.2 Game identity

- `game_id = base32(sha256(system_id + "\0" + rel_path)[:10])`. That's 16 lowercase
  characters, validated with `^[a-z2-7]{16}$`.
- `system_id` is Zaparoo's system id, the same on every platform.
- `rel_path` is the path relative to that system's games folder, normalized: forward
  slashes, NFC Unicode, no leading slash.
- **ASSUMPTION C-RELPATH:** Core's `relativePath` (from `media.meta`, or from search
  results if they carry it) is relative to the system folder and identical on two devices
  that mount the same share.
  - If it isn't, the per-device config key `strip_prefixes` (a list of absolute prefixes)
    derives `rel_path` from the absolute path.
  - Either way, the same ROM must hash to the same id on both MiSTers. A fixture test pins
    this.

### 6.3 SQLite schema (`/var/lib/cartridge/cartridge.db`)

```
systems(id PK, name, manufacturer, release_date)
games(id PK, system_id → systems, title, sort_title, first_seen, updated_at)
games_fts  -- FTS5(title) content=games
device_systems(device, system_id, available INT, media_count INT, synced_at, PK(device, system_id))
device_media(device, game_id → games, media_ref TEXT, abs_path TEXT, present INT,
             synced_at, PK(device, game_id))             -- server-only columns
favorites(game_id PK → games, added_at)
history(device, started_at, game_id NULL, system_id, media_name, play_time,
        PK(device, started_at))                           -- cached from media.history
launches(id PK, device, game_id, requested_at, outcome, detail)   -- Cartridge's own log
art(game_id PK, source, rank, file, content_type, etag, status, fetched_at, retry_after)
                                                -- source: share|zaparoo|libretro|none (§8.1)
sync_runs(id PK, device, started_at, finished_at, status, seen, added, missing, error)
```

- **Games are never deleted.** A game not seen on a device in the latest sync gets
  `present = 0` there. A game present nowhere is hidden unless "show unavailable" is on.
- "Last launched" on a device card is the newest `launches` row with outcome `ok`.

---

## 7. Flows

### 7.1 Catalog sync (per device)

- **When it runs:** at startup (after the first connection), every 6 h, when Core's
  `media.indexing` notification reports completion (ASSUMPTION Z-INDEXING), and on
  **Sync now** (LAN only).
- **One run per device at a time.** A request while one is running joins the running one.
- Steps:
  1. `systems {all: true}` → upsert `systems` and `device_systems.available`. A system is
     available if its launcher exists, even when its media count is 0.
  2. Page `media.search {systems: [s], maxResults: 500, cursor}` per system until done.
     - If results don't carry `relativePath`, batch `media.meta {items: [...≤100]}`
       (ASSUMPTION Z-SEARCH-FIELDS).
     - Upsert `games` and `device_media`.
  3. Mark everything on this device not seen in this run `present = 0`. Write `sync_runs`.
     Push `sync.finished` on the EventHub.
- Cartridge never calls `media.generate`; reindexing is done on the device.
- If no device has synced yet, the UI explains how to index (README link). If the latest
  run failed, the device card shows the error sentence and the last good time.

### 7.2 History and now-playing

- **Now playing.**
  - `media.active` on connect, then `media.started` / `media.stopped` notifications.
  - It's resolved to a `game_id` through `device_media.abs_path` (Zaparoo's
    `mediaPath` matches the indexed path, ASSUMPTION Z-HISTORY-PATH).
  - If it can't be resolved, the device card shows `mediaName` without a link.
- **History.**
  - `media.history {limit: 50, distinctMedia: true}` on connect and after each
    `media.stopped`, upserted into `history`.
  - **Recently played** merges every device's history newest first, tagged by device. It
    includes games started from the MiSTer menu or an NFC card.
  - An offline device's history is served from the cache with its "as of" time.

### 7.3 Launch

Request: `POST /api/launch {device, game, replace: bool, request_id: uuid}`. LAN app only.

Validation happens in this order, and nothing reaches a device until every check passes. A
test asserts that the fake recorded zero calls for each rejection.

| # | Check | Failure → HTTP, `code` |
|---|---|---|
| 1 | Guards (§9): Host, Origin, `X-Cartridge: 1`, JSON body schema | 400/403 `bad_request` |
| 2 | `device` is a configured name | 404 `unknown_device` |
| 3 | `game` matches the id pattern and exists | 404 `unknown_game` |
| 4 | Game present on that device (`device_media.present`) | 409 `not_on_device` |
| 5 | Its system is available on that device | 409 `core_missing` |
| 6 | Device online | 503 `offline` |
| 7 | Rate limit (per device: burst 4, 1 per 15 s, shared bucket with return-to-menu) | 429 `rate_limited` (+ `Retry-After`) |
| 8 | `request_id` seen in the last 60 s → return the first result (double tap) | — |
| 9 | Per-device launch lock (one launch/stop in flight) | 409 `busy_launching` |
| 10 | Device playing something and `replace` is false | 409 `playing` + `{now_playing}` |

Then:
1. Re-read `media.meta {mediaId}` and take `zapScript`.
   - If `isMissing` is true, return 410 `gone`: "the file is gone from this device; sync
     again".
   - The stored `media_ref` goes stale when the device reindexes. If `media.meta` reports
     the id unknown, or returns a different `rel_path` than the one stored, Cartridge
     resyncs that device once and retries the lookup by `rel_path`. If the game is still
     missing after that, the result is 410 `gone`.
2. If replacing, call `stop` and wait for `media.stopped`, or for `media.active == null`
   within 5 s (ASSUMPTION Z-STOP-MENU).
3. Call `run {text: zapScript}` with a 35 s client timeout.
4. Map the outcome:

   | Outcome | Response |
   |---|---|
   | `media_not_found` | 410 `gone` |
   | `busy` | 409 `device_busy` |
   | `blocked` | 409 `blocked` (playtime limits etc.) |
   | `launch_repair` | 502 `launch_failed` + `reason` |
   | timeout | 504 `timeout` |
   | success | 200 `{device, game, started: true}` |
5. Write a `launches` row for every outcome.

**UI side of 10.** The confirm sheet says "*{title}* is running on {label}. Replace it?
Unsaved progress will be lost." On confirm, the client resends with `replace: true`.

**Return to menu** (`POST /api/devices/{name}/menu`) follows checks 1, 2, 6, 7 and 9, then
calls `stop`. The UI always confirms first.

---

## 8. Artwork

**Principle: the share is the source of truth for artwork, and Cartridge does not scrape.**
- The owner scrapes the ROM share once, with a scraper that matches by file hash, using
  their own account (README "Artwork: scrape your share", §14). That puts ES-style
  `gamelist.xml` files and image folders next to the ROMs.
- Every consumer then reads the same files: Zaparoo Core's built-in `gamelist.xml` scraper
  (§3), a Batocera install that uses the share, and Cartridge.
- Each game is scraped once against the owner's quota, not once per app. Online sources in
  Cartridge only fill gaps.

**Cache and serving.**
- Cache directory: `/var/lib/cartridge/art/<aa>/<game_id>.<ext>`.
- Serve path: `/art/<game_id>`, keyed only by the validated id.
- API DTOs carry `art_url: "/art/<game_id>?v=<etag8>"`. The server uses `v` only as a cache
  key and always serves the current bytes, with an `ETag` and
  `Cache-Control: public, max-age=31536000, immutable`. An upgraded image (§8.1) gets a new
  URL, so the grid shows it on the next load instead of waiting out a browser cache.

**Fetching is lazy.** Art is fetched on first request, plus a background prefetch after a
sync, at concurrency 2.

### 8.1 Sources, ranks and upgrades

| Order | Source (`art.source`) | Rank | Notes |
|---|---|---|---|
| 1 | **Share reader** (`share`), only when `ART_SHARE_DIR` is set | 1 | Reads the scraped share directly from a read-only mount (§8.2). It is a cheap local read, it works while every device is offline, and it covers the case where `media.image` doesn't surface `gamelist.xml` art (ASSUMPTION Z-IMAGE-GAMELIST fails) |
| 2 | **Zaparoo** (`zaparoo`): `media.image {mediaId, imageTypes: ["boxart","cover"], maxSize: 512, delivery: "inline"}` from any online device that has the game | 1 | The primary path when no share mount is configured. Core picks up `gamelist.xml` and media folders when it builds its media database (ASSUMPTION Z-IMAGE-GAMELIST). Art scraped later appears only after the owner rebuilds that database in Core's UI, because Cartridge never sends `media.generate` (§9.3) |
| 3 | **libretro-thumbnails** (`libretro`): `https://thumbnails.libretro.com/<Playlist Name>/Named_Boxarts/<Name>.png` | 2 | `<Name>` is the No-Intro-style title with ``&*/:`<>?\|"`` replaced by `_` (ASSUMPTION L-THUMB-URL). The system → playlist-name map is a static table in `art.py`, covering only the systems in fixtures and the common MiSTer cores; unknown systems skip this source. One request per second, generic User-Agent `cartridge/<version>`, honor 429/`Retry-After`. Skipped when `ART_ONLINE=0` |
| 4 | **Generated tile** (`none`) | 3 | SVG rendered server-side: the title on the system's color. Never cached as "found" |

- **Rank-2 and rank-3 results are provisional.** When a device sync succeeds, games whose art
  has rank 2 or 3 get one more try at the rank-1 sources, at concurrency 2. The same retry
  runs when the periodic sync sees a `gamelist.xml` under `ART_SHARE_DIR` with a newer
  mtime; Cartridge polls during sync and never watches the files. A hit replaces the file,
  the `etag` and therefore the `art_url`.
- **Rank-1 results are final.** They are never refetched automatically.
  `cartridge art-refresh [--system <id>]` clears them when the owner rescrapes.
- **Misses don't cost requests on every page view.** A miss is recorded (`status = none`).
  `retry_after` (now + 7 days) gates only the libretro request; rank-1 retries follow the
  sync trigger above.
- **Example.** *Super Metroid* is added to the share before the owner scrapes. The first
  request gets the libretro box art (rank 2). The owner scrapes that night. Then either:
  - the next sync sees the newer `SNES/gamelist.xml`, the share reader finds
    `./images/Super Metroid (Japan, USA)-thumb.png`, and the game moves to `share`
    (rank 1); or
  - with no share mount, the move happens on the first sync after the owner rebuilds Core's
    media database.

  Either way, `art_url` changes from `?v=3f9a…` to `?v=c21e…`.

### 8.2 Share reader (`gamelist.py`)

- **Off by default.** It is enabled by `ART_SHARE_DIR`, the directory that holds the system
  folders as mounted **read-only** in Cartridge's container, for example `/mnt/games` for
  the share the MiSTers mount at `/media/fat/games`.
  - The container never gets a writable mount.
  - Scraping runs elsewhere, with the scraper's credentials (§14).
- **Locating a game.**
  - The system folder is the path segment immediately before the game's `relativePath` in
    the device's `abs_path`. Both are server-only (§6.3, ASSUMPTION C-RELPATH). For
    example, `/media/fat/games/SNES/Super Metroid (Japan, USA).sfc` with `relativePath`
    `Super Metroid (Japan, USA).sfc` gives the folder `SNES`.
  - The reader opens `<ART_SHARE_DIR>/SNES/gamelist.xml` and matches the `<game>` whose
    `<path>`, with any leading `./` removed and normalized to NFC, equals that
    `relativePath`.
- **Picking an image.** It takes `<thumbnail>` first, then `<image>` (ASSUMPTION
  G-LAYOUT: `gamelist.xml` sits in each system folder, its paths are relative to that
  folder, and box art is in one of those two elements, as Batocera and ES-style scrapers
  write them).
- **Containment and parsing.**
  - Every image path is resolved with `os.path.realpath` and must stay inside the system
    folder's own realpath. This rejects `../`, absolute paths and symlink escapes.
  - Only regular files are read.
  - `gamelist.xml` is parsed with `defusedxml`, after a 32 MB size cap.
  - Images are capped at 5 MB.
  - The content type comes from magic bytes (PNG, JPEG, WebP or GIF), never from the
    extension. Anything else is a miss.
- **Caching.** Parsed gamelists are cached in memory, keyed by `(path, mtime)`.
- **What leaves the server.** Bytes are copied into the art cache. No share path ever
  reaches a DTO, a log line or the `art` table, which stores cache-relative names only. The
  §9.1 leak test covers this.
- **A missing or unreadable mount** disables the reader with a sentence in `check` and in the
  `/healthz` detail (§12). The other sources keep working.

### 8.3 Terms and tests

- **Licensing.** Art the owner scraped carries that service's terms; ScreenScraper media,
  for example, is for personal use. Art from libretro-thumbnails comes from a repo with no
  explicit license. Cartridge serves all of it only on the LAN and to Access-allowed users.
  The remote view is never public.
- **Tests never fetch the internet.**
  - httpx goes through a `MockTransport`.
  - The share reader runs against a fixture share built in `tmp_path`, with generated
    images.
  - No downloaded or scraped artwork is committed.

---

## 9. Security model

1. **No paths to the browser.**
   - API DTOs are explicit `TypedDict`s with no path, ref or host fields.
   - A test walks every JSON response, both apps and all fixtures, and fails if it finds any
     fixture `abs_path` or `rel_path` substring, `/media/`, `ART_SHARE_DIR`, or a
     configured host.
2. **No client-chosen targets.** Devices come only from config, and only by name. Every
   launch is validated in the §7.3 order.
3. **The Zaparoo allow-list.**
   - Allowed: `version`, `media`, `media.search`, `media.meta`, `media.image`,
     `media.active`, `media.history`, `systems`, `launchers`, `run`, `stop`.
   - Never `unsafe`. Nothing under `settings.*`, `device.*`, `update.*`, `clients.*`,
     `profiles.*`, `input.*`, `readers.*`, `mappings.*`, and no `media.generate`.
   - `test_safety.py` pins the set.
4. **The remote app** (the tunnel target):
   - Read routes only.
   - Every request needs a valid `Cf-Access-Jwt-Assertion`. The signature is checked
     against the team's JWKS, along with AUD, issuer and expiry, plus
     `REMOTE_ALLOWED_EMAILS` if set.
   - The app **refuses to start** when `REMOTE_ENABLED=1` and the Access settings are
     missing.
   - `/healthz` is answered before auth, with no data.
   - Bind defaults to `127.0.0.1`. If cloudflared runs elsewhere on the LAN, the bind is set
     to the CT's address, and the JWT is the gate.
5. **The LAN app** (unauthenticated by design):
   - **Refuses proxied requests** (any of `cf-connecting-ip`, `cf-ray`, `x-forwarded-for`,
     `forwarded`, `via`, …) with 403. If someone points the tunnel at the LAN port by
     mistake, it fails closed. This is the same rule as shared `remote.py`.
   - **Host allow-list** (`LAN_HOSTS`, default: the bind address, `localhost`, and the
     machine's hostname) against DNS rebinding.
   - **Writes** require `Content-Type: application/json`, the custom header
     `X-Cartridge: 1`, and an `Origin` that is absent or matches an allowed host. Together
     these block cross-site POSTs from other pages on a phone.
   - No CORS headers are ever sent.
6. **Rate limits.**
   - Launch and menu share a bucket per device (§7.3).
   - Sync now: one in flight per device, and at most once a minute.
   - SSE: at most 50 concurrent clients per app.
7. **Logging.** Logs go to stderr through `logsafe` (IPs and MACs redacted), with no
   request bodies. Systemd/OpenRC captures them.
8. **Static files.**
   - A strict CSP: `default-src 'self'; img-src 'self' data:; connect-src 'self'`.
   - No inline script, no third-party fonts or CDNs.

---

## 10. UI

- **Shell.**
  - A sticky header holds the search field and the **target selector**: a pill showing the
    selected device's label, model and status dot. Tapping it opens a sheet listing every
    device with status, now playing, last launched, Return to menu, and Sync now.
  - With one configured device, it's selected automatically. With two or more, nothing is
    selected until the user picks one.
  - The choice is remembered per browser in `localStorage`, wrapped in `try/catch`. If
    storage isn't available, the choice holds for the session only.
  - The server **always** requires `device`; the browser's choice is a convenience, never an
    implicit target.
- **Grid.**
  - Box art with a fixed 3:4 frame (`object-fit: contain`), title below it, lazy-loaded
    images.
  - At least 3 columns at 412 px with no horizontal scroll; more columns as the width grows.
  - Filter chips: systems (only those with present games), Favorites, Recently played, A–Z.
  - "Only games for {device}" is on by default. Unavailable games are dimmed with a reason
    ("not on this device", "core not installed").
- **Game sheet.**
  - Art, title, system, which devices have it, and a star.
  - A large **Play on {label}** button, disabled with a reason when the device is offline,
    the game isn't on it, or no device is selected.
  - In the remote app the sheet has no Play button. It shows "Browse only: launch from
    home".
- **TV.**
  - A "TV mode" toggle, also turned on by `?tv=1`, adds larger tiles, a visible focus ring,
    and spatial arrow-key navigation (`focus.js`).
  - Enter selects; Escape and Backspace close a sheet.
  - Every control is reachable without a pointer.
- **Live updates.** One SSE stream (`/api/events`) carries device status, now-playing and
  sync events, and the client reconnects with backoff.
- **Look.**
  - A dark "arcade" palette of its own, set in the implementation with the frontend-design
    skill.
  - WCAG AA contrast, a bundled variable font, and system colors used for chips and
    placeholder tiles.
  - Never color alone: every chip carries text.

---

## 11. Shared code

- **Synced from `mcp-server-shared`**, never edited here, with the `shared-drift` CI job as
  in the siblings:
  - `logsafe.py`, `tasks.py` and `ratelimit.py`, plus their tests.
  - `scripts/sync_shared.py` and `shared.lock`, with `exclude = ["remote"]`, because
    `remote.py` imports `mcp` and is MCP-specific.
- **Separate PRs to `mcp-server-shared`**, not part of this build:
  1. A generic `zaparoo.py` client lifted from `mcp-server-misterfpga`:
     - It takes `allowed_methods`, a setup hint and an error factory from the caller, so the
       shared code carries no policy.
     - The fake Core server comes with it.
  2. An `access.py` split out of `remote.py`, holding `AccessPolicy`, `AccessVerifier` and
     the ASGI middleware. `remote.py` then imports it.
  
  Until each lands, Cartridge carries local copies (`zaparoo_client.py`, `access.py`)
  derived from the sibling, which is the same author and MIT-licensed. A header comment
  names the pending promotion. Switching later is a file replacement plus a `shared.lock`
  bump.
- **Batocera later.**
  - Zaparoo Core on Batocera has legacy access, so the first attempt is `ZaparooTarget` with
    `platform: batocera`. Batocera's ROM folder names differ, so `rel_path` uses
    `strip_prefixes`.
  - If Core on Batocera can't launch reliably, `EmulationStationTarget` wraps
    `mcp-server-batocera`'s `EmulationStationClient`, either copied or promoted the same
    way.
  - Neither target changes the web layer.

---

## 12. Errors and degraded states

| Situation | Behavior |
|---|---|
| Device off / unreachable | Card shows "offline" + the target's sentence ("No answer from Office: is it on and on the network?"); its games stay browsable from cache; Play disabled |
| Core not installed / not running (connection refused) | Sentence points at README "Install Zaparoo Core" |
| Core demands encryption (`-32002`) | Card shows "needs pairing (Zaparoo encryption is on)"; `auth: paired` is the fix once implemented; until then the README says how to set `encryption = false` |
| Never synced | Empty state explaining indexing in Zaparoo's UI and **Sync now** |
| Sync failed midway | Previous data kept (no `present` changes from a failed run); error on the card |
| Art unavailable | Generated tile (rank 3), upgraded later per §8.1 |
| `ART_SHARE_DIR` missing or unreadable | Share reader off; `check` and the `/healthz` detail say "Artwork share not readable at ART_SHARE_DIR: is it mounted read-only in this container?"; other art sources continue |
| Remote app, Access misconfigured | 403 for everything but `/healthz`; startup refuses if settings missing |
| Database locked/corrupt | `/healthz` reports not-ready; `check` says what to do |

Errors reach the UI as `{code, message}`. `message` is a sentence a person can act on, and
`code` is stable for tests.

---

## 13. Testing (all hardware-free)

- **Runner:** pytest with the anyio plugin (asyncio), plus httpx `ASGITransport` against
  both apps.
- **Simulator:** `FakeZaparoo` is an aiohttp WebSocket server speaking real JSON-RPC:
  ping/pong, cursor pagination, `media.meta` batches, `media.image` and notifications. Its
  failure switches are offline, refuse connection, `-32002`, slow `run`, the `run` error
  categories, and reindex (new media ids).
  - Fixtures: `family-room` and `office` share most games. `office` lacks one core, one ROM
    is on `family-room` only, and one fixture game has awkward characters (`&`, `:`, a
    non-ASCII letter).
- **Must-have tests:**
  - **Identity:** the same ROM on two fixture devices gets one `game_id`, and the id is
    stable across a reindex.
  - **Leak test (§9.1):** no paths or hosts in any API response.
  - **Remote app:** every write route returns 404. No JWT → 403, bad AUD → 403, expired →
    403, valid → 200. Startup refusal without settings.
  - **LAN guards:** forwarded headers → 403; foreign Host → 403; POST without
    `X-Cartridge`, with a wrong `Origin`, or with a form content type → 403.
  - **Launch:** each row of §7.3 is rejected with zero device calls; replace flow;
    double-tap idempotency; rate limit; `media_not_found` after reindex triggers one resync.
  - **Allow-list:** pinned; sending anything else raises before the socket.
  - **Sync:** pagination, `present` flips, a failed run changes nothing, availability from
    `systems {all: true}`.
  - **History and now-playing:** resolution to game ids, and offline cache.
  - **Art chain:** source order and ranks, miss caching, `Retry-After` honored, no network
    in tests.
  - **Upgrades:** a libretro-backed game moves to `share` after a sync once its
    `gamelist.xml` mtime is newer, and to `zaparoo` after the fake device starts returning
    an image. `art_url` changes on upgrade. Rank-1 art is not refetched until
    `art-refresh`.
  - **Share reader:** matches by `relativePath`, including the `&`, `:` and non-ASCII
    fixture names. Rejects `../`, absolute paths, symlink escapes, non-regular files,
    oversize files and non-image bytes. Is off with `ART_SHARE_DIR` unset. Reports a
    missing mount through `check`.
  - **Config:** validation sentences; a bad device doesn't block the others.
  - **Assumptions registry:** every id is cited in code with `# ASSUMPTION <id>` and listed
    in README and HARDWARE_VALIDATION.md (sibling pattern).
- **UI smoke:** Playwright (Chromium) against `cartridge simulate`, at 412×915 (phone) and
  1920×1080 with `?tv=1`, in a separate CI job. It loads the grid, filters, selects a
  device, launches, confirms replace, and checks for no horizontal scroll.
- **Static checks:** `node --check` on every `static/*.js`.

### Assumptions to verify on hardware (`HARDWARE_VALIDATION.md`)

| Id | Claim |
|---|---|
| Z-LEGACY | MiSTer Core admits unauthenticated plaintext LAN clients for every allow-listed method (incl. `media.image`) |
| Z-ENDPOINT | `ws://<host>:7497/api/v0.1`, string ids, `ping`→`pong` |
| Z-SEARCH-FIELDS | `media.search` results carry `mediaId`, `name`, `systemId`, `path` (and maybe `relativePath`) |
| C-RELPATH | `relativePath` is system-folder-relative and identical across devices on one share |
| Z-RUN-WAITS | `run` returns after the launch, ≤ 30 s |
| Z-STOP-MENU | `stop` returns MiSTer to the menu and `media.stopped` follows |
| Z-HISTORY-PATH | `media.history.mediaPath` / `media.active.mediaPath` equal the indexed path |
| Z-INDEXING | `media.indexing` signals completion in a detectable way |
| Z-IMAGE | `media.image` inline returns base64 + `contentType`, and an error/empty result when no art |
| Z-ENC-ERROR | Encryption-required rejection is `-32002` |
| L-THUMB-URL | libretro-thumbnails URL and character-replacement rule |
| Z-IMAGE-GAMELIST | After Core rebuilds its media database on a share that holds ES-style `gamelist.xml` and images, `media.image` returns that box art for `imageTypes: ["boxart","cover"]` |
| G-LAYOUT | The scraper writes `gamelist.xml` into each system folder, with `./`-relative `<path>`s that equal Core's `relativePath`, and box art in `<thumbnail>` or `<image>` |

---

## 14. Deployment and repo conventions

- **Target:** an Alpine LXC on Proxmox with OpenRC.
  - `deploy/alpine/`: `install.sh` (idempotent; creates the `cartridge` user and a venv in
    `/opt/cartridge`; keeps `/var/lib/cartridge` and existing config), `cartridge.initd`
    (`supervise-daemon`, `run.sh`), `conf.d.example`, `devices.yaml.example`, and
    `smoke-test.sh`, which starts the app against `simulate` and curls both apps.
  - Paths: config in `/etc/conf.d/cartridge` (0640) and `/etc/cartridge/devices.yaml`;
    state in `/var/lib/cartridge` (0700).
- **Tunnel:** cloudflared ingress `games.example.com → http://<ct>:8771`, behind an Access
  application. The README covers the cloudflared and Access steps generically. **The LAN
  port is never a tunnel target.**
- **Settings (env):**

  | Setting | Default |
  |---|---|
  | `LAN_BIND` | `0.0.0.0:8770` |
  | `LAN_HOSTS` | — |
  | `REMOTE_ENABLED` | `1` |
  | `REMOTE_BIND` | `127.0.0.1:8771` |
  | `CF_ACCESS_TEAM_DOMAIN` | — |
  | `CF_ACCESS_AUD` | — |
  | `REMOTE_ALLOWED_EMAILS` | — |
  | `DATA_DIR` | `/var/lib/cartridge` |
  | `DEVICES_FILE` | — |
  | `ART_ONLINE` | `1` |
  | `ART_SHARE_DIR` | — (share reader off) |
  | `SYNC_INTERVAL_HOURS` | `6` |
  | `LOG_LEVEL` | — |
- **Project tooling:**
  - Python ≥ 3.11, `pyproject.toml` (setuptools), package `cartridge` with console script
    `cartridge`.
  - Dependencies: `starlette`, `uvicorn`, `aiohttp`, `httpx`, `pyjwt[crypto]`, `pyyaml`,
    `defusedxml`.
  - Dev: `pytest`, `anyio`, `ruff`, `mypy`, `playwright`, `types-PyYAML`.
  - ruff (E, W, F, I, UP, B, SIM; line 120) plus `ruff format`; mypy `strict = true`.
- **CI** (`.github/workflows/ci.yml`):
  - Tests, lint and typecheck on Python 3.11–3.14.
  - A `python:3.12-alpine` smoke job running `deploy/alpine/smoke-test.sh`.
  - A Playwright job.
  - The `shared-drift` job.
  - A weekly schedule and `workflow_dispatch`.
- **Verification gate** (written into CLAUDE.md; run before every completion claim):

  ```sh
  python -m pytest && python -m ruff check . && python -m ruff format --check . && python -m mypy && python scripts/check_js.py
  ```
  `check_js.py` runs `node --check` on each static JS file. The Playwright job runs in CI;
  locally it runs with `python -m pytest -m e2e`.
- **CLAUDE.md** (first plan task) captures:
  - this file's guardrails: §9, the allow-list, the never-guess-a-device rule, the
    shared-file rule, and the assumption tagging;
  - the module map and the gate.
- **README:** what it is; screenshots from the simulator; **Install Zaparoo Core on a
  MiSTer**; device config; deploy on Alpine; cloudflared and Access; **Artwork: scrape your share**
  (below) and each art source's terms; Home Assistant tracking via `mcp-server-misterfpga track`; the assumptions
  table; and `cartridge simulate` for trying it without hardware.
  - **Install Zaparoo Core on a MiSTer** (it isn't installed by default):
    1. Run Update All, press UP during the countdown, turn on **Zaparoo** under **Tools &
       Scripts**, then SAVE.
    2. Open **Scripts → zaparoo** on the MiSTer and accept starting it as a service.
    3. Open `http://<mister-ip>:7497/app/` and build the media database.
    4. Don't configure a Zaparoo API key.
  - **Artwork: scrape your share.** This is setup documentation; Cartridge runs none of it.
    1. Why: one scrape, matched by file hash, serves Zaparoo, Batocera and Cartridge, and
       spends the scraper quota once.
    2. Worked example with Skyscraper (the maintained Gemba fork), run on a separate host
       or container that mounts the share **read-write**, using the owner's ScreenScraper
       account. It runs per system with `-s screenscraper` into Skyscraper's cache, then
       without `-s` to write a Batocera-style gamelist (`-f batocera`) and media into that
       system folder (`-i`, `-g`, `-o`).
    3. A short checklist to confirm G-LAYOUT on the first system before scraping the rest.
    4. Rebuild Core's media database on each MiSTer (`http://<mister-ip>:7497/app/`) so
       `media.image` sees the new art (Z-IMAGE-GAMELIST).
    5. Optionally mount the share read-only into Cartridge's container and set
       `ART_SHARE_DIR`.
    6. Cartridge's container never holds scraper credentials or a writable mount.
- **Git:**
  - Commits are authored with `SDNick484@users.noreply.github.com`.
  - Work happens on branches with PRs into `main`.
  - Licensed MIT. Zaparoo Core is GPL-3.0: Cartridge uses only its documented protocol and
    copies no code.

---

## 15. Out of scope for v1

- The Batocera target (the Protocol and config field exist).
- `auth: paired`. The transport seam and the `-32002` handling exist, and a `cartridge pair`
  CLI comes later.
- ScreenScraper or any other credentialed scraper **inside Cartridge**. Scraping is the
  owner's step on the share (§8, §14 README); Cartridge only reads the result.
- Writing to the share.
- Per-person profiles; favorites and history are household-wide.
- MQTT and Home Assistant publishing (D5).
- Triggering a Core reindex (`media.generate`), NFC, editing metadata, managing ROMs.
- Remote launching, in any form.
