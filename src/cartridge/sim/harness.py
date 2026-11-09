"""Run simulated MiSTers (one FakeZaparoo each) and point a devices file at them.

Used by the tests and by ``cartridge simulate``, so both exercise the same fakes. Each fake binds
127.0.0.1 on its own port (not a loopback alias per device) so it works on every platform.
Derived from mcp-server-misterfpga's sim/harness.py (MIT, same author).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fake_zaparoo import FakeZaparoo, load_fixture

HOST = "127.0.0.1"


@dataclass
class SimDevice:
    name: str  # the id in devices.yaml: the fixture file stem with _ replaced by -
    fake: FakeZaparoo


@dataclass
class Simulation:
    root: Path  # scratch folder for the caller's files (the devices file, a database)
    devices: list[SimDevice] = field(default_factory=list)

    async def add(self, fixture: str, port: int = 0) -> SimDevice:
        fake = FakeZaparoo(load_fixture(fixture), HOST, port)
        await fake.start()
        dev = SimDevice(fixture.replace("_", "-"), fake)
        self.devices.append(dev)
        return dev

    async def stop(self) -> None:
        for dev in self.devices:
            await dev.fake.stop()

    def devices_yaml(self) -> str:
        """The devices file (spec 6.1) that points a server at these fakes."""
        entries = [
            {
                "name": d.name,
                "label": d.fake.name,
                "model": d.fake.model,
                "platform": "mister",
                "transport": "zaparoo",
                "host": HOST,
                "port": d.fake.port,
                "auth": "legacy",
            }
            for d in self.devices
        ]
        return yaml.safe_dump({"devices": entries}, sort_keys=False, allow_unicode=True)

    def write_devices(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.devices_yaml(), encoding="utf-8")
        return path
