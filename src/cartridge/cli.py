"""Command line: serve (default), check, sync and simulate (spec 5, 9.4).

``serve`` runs the LAN and remote apps as two uvicorn servers in one event loop. Configuration that
would leave the server open or unusable is refused up front with a sentence and exit code 2.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import Callable, Coroutine, Generator, Mapping, Sequence
from pathlib import Path
from typing import Any

import uvicorn
from starlette.types import ASGIApp

from cartridge import __version__
from cartridge.access import AccessVerifier, policy_from_settings
from cartridge.config import LOG_LEVELS, Devices, Settings, load_devices, load_settings
from cartridge.core import Background, build_core, close_core, database_path, open_database
from cartridge.errors import ConfigError
from cartridge.logsafe import setup_logging
from cartridge.registry import DeviceRegistry
from cartridge.sim.harness import Simulation
from cartridge.web.lan import build_lan_app, default_lan_hosts
from cartridge.web.remote import build_remote_app

log = logging.getLogger(__name__)

DEFAULT_SIM_BIND = "127.0.0.1:8770"
SETTLE_SECONDS = 5.0
SERVER_STOP_SECONDS = 10.0


class _Server(uvicorn.Server):
    """uvicorn's own signal capture would fight over two servers and re-raise the signal; `_wait_for_stop` owns them."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def _setup_logging(env: Mapping[str, str]) -> None:
    level = env.get("LOG_LEVEL", "INFO").strip().upper()
    setup_logging(getattr(logging, level if level in LOG_LEVELS else "INFO"))


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    return stop


def _load(env: Mapping[str, str]) -> tuple[Settings, Devices]:
    settings = load_settings(env)
    return settings, load_devices(settings.devices_file)


def _db_advice(settings: Settings, why: object) -> str:
    path = database_path(settings)
    return f"The database can't be used ({why}): delete or restore {path}; Cartridge rebuilds it on the next sync."


async def _settle(registry: DeviceRegistry) -> None:
    """Wait (briefly) until every device has had its first connection attempt."""
    for _ in range(int(SETTLE_SECONDS / 0.05)):
        if all(registry.status(n).state != "connecting" for n in registry.names()):
            return
        await asyncio.sleep(0.05)


# -- serve ---------------------------------------------------------------------------------------


async def _serve(env: Mapping[str, str]) -> int:
    settings, devices = _load(env)
    # Built before anything starts, so bad Access settings refuse the whole server.
    verifier = AccessVerifier(policy_from_settings(settings)) if settings.remote_enabled else None
    try:
        core = await build_core(settings, devices)
    except (sqlite3.Error, OSError) as exc:
        print(_db_advice(settings, exc), file=sys.stderr)
        return 1
    Background(core).start()
    hosts = settings.lan_hosts or default_lan_hosts(settings.lan_bind[0])
    apps: list[tuple[ASGIApp, tuple[str, int]]] = [(build_lan_app(core, hosts), settings.lan_bind)]
    if verifier is not None:
        apps.append((build_remote_app(core, verifier), settings.remote_bind))
    servers = [
        _Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                lifespan="off",
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )
        for app, (host, port) in apps
    ]
    stop = _stop_event()
    failed = False

    async def run(server: _Server) -> None:
        nonlocal failed
        try:
            await server.serve()
        except SystemExit:  # uvicorn exits instead of raising when it can't bind
            log.error("Could not listen on %s:%s; is the port already in use?", server.config.host, server.config.port)
            failed = True
        stop.set()

    runs = [asyncio.create_task(run(s)) for s in servers]
    log.info("Cartridge %s listening on %s.", __version__, ", ".join(f"{h}:{p}" for _, (h, p) in apps))
    await stop.wait()
    core.hub.close()  # first, so open SSE streams end and the servers can drain
    for server in servers:
        server.should_exit = True
    await asyncio.wait(runs, timeout=SERVER_STOP_SECONDS)
    await close_core(core)  # stops Background first
    return 1 if failed else 0


def _cmd_serve(env: Mapping[str, str]) -> int:
    try:
        return asyncio.run(_serve(env))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2


# -- check and sync ------------------------------------------------------------------------------


async def _check(env: Mapping[str, str]) -> int:
    settings, devices = _load(env)
    ok = True
    for problem in devices.problems:
        print(problem.sentence)
        ok = False
    registry = DeviceRegistry(devices)
    await registry.start()
    try:
        await _settle(registry)
        for name in registry.names():
            status = registry.status(name)
            print(f"{name}: {status.state}" + (f" - {status.message}" if status.message else ""))
            ok = ok and status.online
    finally:
        await registry.close()
    try:
        db = open_database(settings)
        try:
            healthy = db.ping()
        finally:
            db.close()
        if not healthy:
            raise sqlite3.DatabaseError("it didn't answer a query")
        print("database: ok")
    except (sqlite3.Error, OSError) as exc:
        print(_db_advice(settings, exc))
        ok = False
    return 0 if ok else 1


async def _sync(env: Mapping[str, str], only: str | None) -> int:
    settings, devices = _load(env)
    core = await build_core(settings, devices)
    try:
        await _settle(core.registry)
        names = [only] if only else core.registry.names()
        failed = False
        for name in names:
            if core.registry.target(name) is None:
                info = core.registry.info(name)
                print(f"{name}: {info.problem if info and info.problem else 'not a configured device'}")
                failed = True
                continue
            result = await core.catalog.sync(name)
            if result.status == "ok":
                print(f"{name}: ok ({result.seen} seen, {result.added} added, {result.missing} missing)")
            else:
                print(f"{name}: failed - {result.error}")
                failed = True
        return 1 if failed else 0
    finally:
        await close_core(core)


def _run_guarded(make: Callable[[], Coroutine[Any, Any, int]], env: Mapping[str, str]) -> int:
    try:
        return asyncio.run(make())
    except ConfigError as exc:
        print(exc, file=sys.stderr)
    except (sqlite3.Error, OSError) as exc:
        print(f"{exc}", file=sys.stderr)
    return 1


# -- simulate ------------------------------------------------------------------------------------


async def _simulate(directory: Path, fakes_only: bool, lan_bind: str, env: Mapping[str, str]) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    sim = Simulation(directory)
    try:
        await sim.add("family_room")
        await sim.add("office")
        devices_file = sim.write_devices(directory / "devices.yaml")
        if fakes_only:
            stop = _stop_event()
            print(devices_file, flush=True)
            await stop.wait()
            return 0
        merged = {
            **env,
            "REMOTE_ENABLED": env.get("REMOTE_ENABLED", "0"),
            "DATA_DIR": str(directory / "data"),
            "DEVICES_FILE": str(devices_file),
            "LAN_BIND": lan_bind,
        }
        print(f"Cartridge simulator: http://{lan_bind}/", flush=True)
        return await _serve(merged)
    finally:
        await sim.stop()


# -- entry point ---------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cartridge", description="Browser game launcher for MiSTer devices.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web apps (the default)")
    sub.add_parser("check", help="check the configuration, each device and the database")
    sync = sub.add_parser("sync", help="sync device catalogs now")
    sync.add_argument("--device", help="sync only this device")
    sim = sub.add_parser("simulate", help="run simulated MiSTers and a server pointed at them")
    sim.add_argument("--dir", default="./sim-data", type=Path, help="where to keep the simulator's files")
    sim.add_argument("--fakes-only", action="store_true", help="start only the fake devices and print the devices file")
    sim.add_argument("--lan-bind", default=DEFAULT_SIM_BIND, metavar="HOST:PORT")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = os.environ
    _setup_logging(env)
    command = args.command or "serve"
    if command == "serve":
        return _cmd_serve(env)
    if command == "check":
        return _run_guarded(lambda: _check(env), env)
    if command == "sync":
        return _run_guarded(lambda: _sync(env, args.device), env)
    try:
        return asyncio.run(_simulate(args.dir, args.fakes_only, args.lan_bind, env))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
