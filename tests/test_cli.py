import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from cartridge.cli import main
from cartridge.sim.harness import Simulation

pytestmark = pytest.mark.anyio

TIMEOUT = 20


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def cli_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    own = ("CF_ACCESS", "REMOTE_", "LAN_", "DEVICES_", "DATA_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(own)}
    env.update({"DATA_DIR": str(tmp_path / "data"), "PYTHONUNBUFFERED": "1", **extra})
    return env


async def start_cli(*args: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "cartridge.cli",
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def run_cli(*args: str, env: dict[str, str]) -> tuple[int, str, str]:
    proc = await start_cli(*args, env=env)
    out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


async def test_serve_refuses_remote_without_access_settings(tmp_path: Path) -> None:
    code, out, err = await run_cli("serve", env=cli_env(tmp_path, REMOTE_ENABLED="1"))
    assert code == 2
    assert "CF_ACCESS_TEAM_DOMAIN" in err
    assert out == ""


async def test_serve_is_the_default_subcommand(tmp_path: Path) -> None:
    code, _, err = await run_cli(env=cli_env(tmp_path, REMOTE_ENABLED="1"))
    assert code == 2 and "CF_ACCESS_TEAM_DOMAIN" in err


async def test_serve_refuses_unreadable_devices_file(tmp_path: Path) -> None:
    env = cli_env(tmp_path, REMOTE_ENABLED="0", DEVICES_FILE=str(tmp_path / "missing.yaml"))
    code, _, err = await run_cli("serve", env=env)
    assert code == 2 and "missing.yaml" in err


def devices_with_bad_entry(sim: Simulation, tmp_path: Path) -> Path:
    doc = yaml.safe_load(sim.devices_yaml())
    doc["devices"].insert(1, {"name": "Bad Name", "host": "192.0.2.9"})
    path = tmp_path / "devices.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


async def test_check_reports_problem_and_online_devices(sim: Simulation, tmp_path: Path) -> None:
    path = devices_with_bad_entry(sim, tmp_path)
    code, out, _ = await run_cli("check", env=cli_env(tmp_path, REMOTE_ENABLED="0", DEVICES_FILE=str(path)))
    assert code == 1
    assert "Bad Name: name must be 1–32 characters from [a-z0-9-]." in out
    assert "family-room: online" in out and "office: online" in out


async def test_check_passes_when_everything_is_fine(sim: Simulation, tmp_path: Path) -> None:
    path = sim.write_devices(tmp_path / "devices.yaml")
    code, out, _ = await run_cli("check", env=cli_env(tmp_path, REMOTE_ENABLED="0", DEVICES_FILE=str(path)))
    assert code == 0, out
    assert "database: ok" in out


async def test_check_says_what_to_do_about_a_corrupt_database(sim: Simulation, tmp_path: Path) -> None:
    path = sim.write_devices(tmp_path / "devices.yaml")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cartridge.db").write_bytes(b"this is not a database" * 100)
    code, out, _ = await run_cli("check", env=cli_env(tmp_path, REMOTE_ENABLED="0", DEVICES_FILE=str(path)))
    assert code == 1
    assert f"delete or restore {tmp_path / 'data' / 'cartridge.db'}; Cartridge rebuilds it on the next sync" in out


async def test_sync_one_device(sim: Simulation, tmp_path: Path) -> None:
    path = sim.write_devices(tmp_path / "devices.yaml")
    env = cli_env(tmp_path, REMOTE_ENABLED="0", DEVICES_FILE=str(path))
    code, out, _ = await run_cli("sync", "--device", "office", env=env)
    assert code == 0, out
    assert "office: ok" in out and "family-room" not in out


async def test_sync_all_and_unknown_device(sim: Simulation, tmp_path: Path) -> None:
    path = sim.write_devices(tmp_path / "devices.yaml")
    env = cli_env(tmp_path, REMOTE_ENABLED="0", DEVICES_FILE=str(path))
    code, out, _ = await run_cli("sync", env=env)
    assert code == 0 and "family-room: ok" in out and "office: ok" in out
    code, out, _ = await run_cli("sync", "--device", "nope", env=env)
    assert code == 1 and "nope" in out


async def test_sync_exits_1_when_a_device_is_down(sim: Simulation, tmp_path: Path) -> None:
    path = sim.write_devices(tmp_path / "devices.yaml")
    sim.devices[1].fake.refuse = True
    env = cli_env(tmp_path, REMOTE_ENABLED="0", DEVICES_FILE=str(path))
    code, out, _ = await run_cli("sync", env=env)
    assert code == 1 and "family-room: ok" in out and "office: failed" in out


def test_bad_arguments_use_argparse_exit_code() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


async def test_simulate_serves_games_and_stops_on_sigterm(tmp_path: Path) -> None:
    port = free_port()
    proc = await start_cli(
        "simulate", "--dir", str(tmp_path / "sim"), "--lan-bind", f"127.0.0.1:{port}", env=cli_env(tmp_path)
    )
    try:
        assert proc.stdout is not None
        banner = await asyncio.wait_for(proc.stdout.readline(), 10)
        assert banner.decode().strip() == f"Cartridge simulator: http://127.0.0.1:{port}/"
        total = 0
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            for _ in range(200):
                try:
                    total = (await client.get("/api/games")).json()["total"]
                except (httpx.TransportError, KeyError, ValueError):
                    total = 0
                if total:
                    break
                await asyncio.sleep(0.05)
        assert total > 0
        assert (tmp_path / "sim" / "devices.yaml").exists()
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), 8) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_simulate_fakes_only_prints_path_and_waits(tmp_path: Path) -> None:
    proc = await start_cli("simulate", "--fakes-only", "--dir", str(tmp_path / "sim"), env=cli_env(tmp_path))
    try:
        assert proc.stdout is not None
        line = (await asyncio.wait_for(proc.stdout.readline(), 10)).decode().strip()
        assert line == str(tmp_path / "sim" / "devices.yaml") and Path(line).exists()
        await asyncio.sleep(0.2)
        assert proc.returncode is None
        proc.send_signal(signal.SIGINT)
        assert await asyncio.wait_for(proc.wait(), 8) == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
