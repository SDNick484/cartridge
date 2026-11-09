# CLAUDE.md

## What this is

Cartridge is a self-hosted browser game launcher for MiSTer FPGA devices. It indexes each device's library through
Zaparoo Core, shows it as a cover-art grid, and launches games on a chosen device. Two Starlette apps: a LAN app
(full UI, write routes) and a remote app (read-only, behind Cloudflare Access). The spec is the source of truth; read
`docs/superpowers/specs/2026-11-07-cartridge-design.md` before changing behavior.

## Module map (`src/cartridge/`)

- `errors.py`: `ApiError`, shared by `launch.py` and `web/`.
- `config.py`: env settings and `devices.yaml` loading.
- `ids.py`: stable game ids from system and relative path.
- `db.py`: SQLite schema and queries (FTS search, history, launches).
- `zaparoo_client.py`: Zaparoo Core WebSocket JSON-RPC client; owns the method allow-list.
- `registry.py`: device registry, connection state, availability.
- `events.py`: SSE hub with client cap.
- `catalog.py`: library sync per device; present flags.
- `art.py`: artwork lookup, cache and placeholder tiles.
- `launch.py`: launch validation order and rate limits.
- `access.py`: Cloudflare Access JWT verification for the remote app.
- `core.py`: composes the core and runs background loops (syncs, history ingest, event fan-out).
- `cli.py`: `cartridge serve | check | sync | simulate`.
- `targets/`: `base.py` (`LaunchTarget`), `zaparoo.py` (the Zaparoo target).
- `web/`: `common.py` (shared routes and helpers), `dto.py` (the TypedDict DTOs), `lan.py`, `remote.py`.
- `sim/`: `fake_zaparoo.py`, `harness.py`, `fixtures/*.json` (hardware-free simulator).
- `static/`: browser UI (plain ES modules, no build step; `dom.js` DOM helpers, `sheet.js` launch sheet) and the bundled font.
- `logsafe.py`, `tasks.py`, `ratelimit.py`: synced from mcp-server-shared, never edited.

Outside `src/`: `tests/helpers.py` (shared test helpers), `tests/e2e/` (Playwright, run with `-m e2e`), `deploy/alpine/`
(install, OpenRC service, smoke test), `HARDWARE_VALIDATION.md`.

## Guardrails

- No filesystem paths, refs or device hosts ever reach the browser. DTOs are TypedDicts; `test_leaks` enforces this.
- Devices come only from config and are addressed only by name. Never guess a device: the server always requires
  `device`.
- Launch validation follows the order in spec 7.3, with zero device calls on rejection.
- Zaparoo allow-list is exactly `version, media, media.search, media.meta, media.image, media.active, media.history,
  systems, launchers, run, stop`. `test_safety` pins it; widening it is a spec change. Never `unsafe` or `media.generate`.
- The remote app has no write routes and fails closed on Access (spec 9.4): it refuses to start when
  `REMOTE_ENABLED=1` without `CF_ACCESS_TEAM_DOMAIN` and `CF_ACCESS_AUD`, and `/healthz` is answered before auth.
- Rate limits (spec 9.6): launch and menu share a per-device bucket (burst 4, 1 per 15 s); sync is once a minute with
  one in flight per device; at most 50 SSE clients per app.
- The LAN app refuses proxied requests, enforces the Host allow-list and the write guards, and never sends CORS headers.
- Strict CSP, exactly `default-src 'self'; img-src 'self' data:; connect-src 'self'`: no inline script, no CDNs.
- Log to stderr via `logsafe`; never log request bodies.
- Shared files (`logsafe`, `tasks`, `ratelimit`, their tests, `scripts/sync_shared.py`, `shared.lock`) are synced,
  never hand-edited: `python scripts/sync_shared.py --update`. CI job `shared-drift` enforces it.
- Every unverified protocol claim is a `# ASSUMPTION <id>` comment (ids in spec 13), listed in README and
  `HARDWARE_VALIDATION.md`; `test_assumptions` checks this.
- Generic by design: no personal names, hostnames or domains. Examples use `example.com`, `192.0.2.x`, `family-room`
  and `office`.
- No `media.generate`, no API key; Zaparoo legacy access only.
- Errors reach the UI as `{"code": str, "message": str}` with a message a person can act on.

## Status

v1 is complete and verified against the simulator only; `HARDWARE_VALIDATION.md` is the checklist for real MiSTers.

## Verification gate

Run before every commit and every completion claim, inside the venv:

```sh
python -m pytest && python -m ruff check . && python -m ruff format --check . && python -m mypy && python scripts/check_js.py
```

Browser smoke tests are excluded by default: `python -m playwright install chromium`, then `python -m pytest -m e2e`.
Async tests use the anyio plugin (`pytestmark = pytest.mark.anyio`), never pytest-asyncio. No hardware or internet in
tests.

## Commands

```sh
python3.11 -m venv .venv && . .venv/bin/activate && python -m pip install -e ".[dev]"
cartridge simulate                      # try it without hardware
cartridge check                         # validate config and reach each device
python scripts/sync_shared.py --check   # shared files match the locked commit
```
