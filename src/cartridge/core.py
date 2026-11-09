"""The composed server core: everything the web layers read from and write through."""

from __future__ import annotations

from dataclasses import dataclass

from cartridge.art import ArtCache
from cartridge.catalog import Catalog
from cartridge.config import Settings
from cartridge.db import Database
from cartridge.events import EventHub
from cartridge.launch import LaunchService
from cartridge.registry import DeviceRegistry


@dataclass
class Core:
    settings: Settings
    db: Database
    registry: DeviceRegistry
    catalog: Catalog
    art: ArtCache
    hub: EventHub
    launcher: LaunchService
