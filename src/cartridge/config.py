"""Settings (env) and the device list (YAML), validated into frozen values.

A bad device entry never stops the others: it becomes a DeviceProblem the UI shows on a card.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from .errors import ConfigError

NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    label: str
    model: str
    platform: Literal["mister"]
    transport: Literal["zaparoo"]
    host: str
    port: int = 7497
    auth: Literal["legacy"] = "legacy"
    strip_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceProblem:
    index: int
    name: str | None  # set only when the name is valid and unique, so the card can be keyed by it
    label: str
    sentence: str


@dataclass(frozen=True)
class Devices:
    devices: tuple[DeviceConfig, ...]
    problems: tuple[DeviceProblem, ...]


@dataclass(frozen=True)
class Settings:
    lan_bind: tuple[str, int]
    lan_hosts: frozenset[str] | None
    remote_enabled: bool
    remote_bind: tuple[str, int]
    cf_access_team_domain: str | None
    cf_access_aud: tuple[str, ...]
    remote_allowed_emails: frozenset[str]
    data_dir: Path
    devices_file: Path
    art_online: bool
    sync_interval_hours: float
    log_level: str


def parse_bind(text: str, default_port: int) -> tuple[str, int]:
    """Accept `host:port`, `[v6]:port`, or a bare host (including a bare v6 address)."""
    text = text.strip()
    host, port_text = text, None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ConfigError(f"'{text}' isn't a valid bind address; use host:port or [v6]:port.")
        host = text[1:end]
        tail = text[end + 1 :]
        if tail:
            if not tail.startswith(":"):
                raise ConfigError(f"'{text}' isn't a valid bind address; use host:port or [v6]:port.")
            port_text = tail[1:]
    elif text.count(":") == 1:
        host, port_text = text.split(":")
    if not host:
        raise ConfigError(f"'{text}' has no host; use host:port.")
    port = default_port
    if port_text is not None:
        try:
            port = int(port_text)
        except ValueError:
            raise ConfigError(f"'{text}' has a port that isn't a number; use 1–65535.") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"'{text}' has a port outside 1–65535.")
    return host, port


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return HOST_RE.match(host) is not None
    return True


def _csv(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def load_settings(env: Mapping[str, str]) -> Settings:
    problems: list[str] = []

    def bind(name: str, default: str, port: int) -> tuple[str, int]:
        try:
            return parse_bind(env.get(name, default), port)
        except ConfigError as e:
            problems.append(f"{name}: {e}")
            return parse_bind(default, port)

    def flag(name: str, default: str) -> bool:
        raw = env.get(name, default).strip().lower()
        if raw in ("1", "true"):
            return True
        if raw in ("0", "false"):
            return False
        problems.append(f"{name} is '{env[name]}'; use 1 or 0.")
        return default == "1"

    lan_bind = bind("LAN_BIND", "0.0.0.0:8770", 8770)
    remote_bind = bind("REMOTE_BIND", "127.0.0.1:8771", 8771)
    remote_enabled = flag("REMOTE_ENABLED", "1")
    art_online = flag("ART_ONLINE", "1")

    interval = 6.0
    try:
        interval = float(env.get("SYNC_INTERVAL_HOURS", "6"))
        if not interval > 0:
            raise ValueError
    except ValueError:
        problems.append(f"SYNC_INTERVAL_HOURS is '{env.get('SYNC_INTERVAL_HOURS')}'; use a number of hours above 0.")
        interval = 6.0

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL is '{env.get('LOG_LEVEL')}'; use one of {', '.join(LOG_LEVELS)}.")
        log_level = "INFO"

    team = env.get("CF_ACCESS_TEAM_DOMAIN", "").strip() or None
    aud = tuple(_csv(env.get("CF_ACCESS_AUD", "")))
    if remote_enabled and not (team and aud):
        problems.append(
            "REMOTE_ENABLED is on but CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD aren't both set; "
            "set them to your Cloudflare Access values, or set REMOTE_ENABLED=0 to run LAN-only."
        )

    hosts_raw = env.get("LAN_HOSTS", "").strip()
    lan_hosts = frozenset(h.lower() for h in _csv(hosts_raw)) if hosts_raw else None

    if problems:
        raise ConfigError(" ".join(problems))
    return Settings(
        lan_bind=lan_bind,
        lan_hosts=lan_hosts,
        remote_enabled=remote_enabled,
        remote_bind=remote_bind,
        cf_access_team_domain=team,
        cf_access_aud=aud,
        remote_allowed_emails=frozenset(e.lower() for e in _csv(env.get("REMOTE_ALLOWED_EMAILS", ""))),
        data_dir=Path(env.get("DATA_DIR", "/var/lib/cartridge")),
        devices_file=Path(env.get("DEVICES_FILE", "/etc/cartridge/devices.yaml")),
        art_online=art_online,
        sync_interval_hours=interval,
        log_level=log_level,
    )


def _entry_label(index: int, raw: object) -> str:
    if isinstance(raw, dict):
        for key in ("label", "name"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return f"entry #{index + 1}"


def _check_entry(label: str, raw: dict[str, Any], seen: set[str]) -> tuple[DeviceConfig | None, list[str], str | None]:
    """Return (device if clean, sentences, name if valid and unique)."""
    errs: list[str] = []
    name = raw.get("name")
    good_name: str | None = None
    if not isinstance(name, str) or not NAME_RE.match(name):
        errs.append(f"{label}: name must be 1–32 characters from [a-z0-9-].")
    elif name in seen:
        errs.append(f"{label}: the name '{name}' is already used by an earlier entry; give each device its own name.")
    else:
        good_name = name

    host = raw.get("host")
    if not isinstance(host, str) or not _valid_host(host):
        errs.append(f"{label}: host must be an IP address or a hostname.")

    port = raw.get("port", 7497)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        errs.append(f"{label}: port must be a number in 1–65535.")

    platform = raw.get("platform", "mister")
    if platform != "mister":
        if platform == "batocera":
            errs.append(f"{label}: platform batocera isn't supported in v1; use mister.")
        else:
            errs.append(f"{label}: platform '{platform}' isn't known; use mister.")
    transport = raw.get("transport", "zaparoo")
    if transport != "zaparoo":
        errs.append(f"{label}: transport '{transport}' isn't known; use zaparoo.")
    auth = raw.get("auth", "legacy")
    if auth == "paired":
        errs.append(f"{label}: auth: paired isn't implemented yet; use legacy.")
    elif auth != "legacy":
        errs.append(f"{label}: auth '{auth}' isn't known; use legacy.")

    prefixes = raw.get("strip_prefixes", [])
    if not isinstance(prefixes, list) or not all(isinstance(p, str) and p.startswith("/") for p in prefixes):
        errs.append(f"{label}: strip_prefixes must be a list of absolute paths starting with /.")
        prefixes = []

    label_text = raw.get("label", name)
    model = raw.get("model", "")
    if errs or good_name is None:
        return None, errs, good_name
    return (
        DeviceConfig(
            name=good_name,
            label=str(label_text),
            model=str(model),
            platform="mister",
            transport="zaparoo",
            host=str(host),
            port=port,
            auth="legacy",
            strip_prefixes=tuple(prefixes),
        ),
        errs,
        good_name,
    )


def load_devices(path: Path) -> Devices:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"Can't read the device file {path}: {e.strerror or e}.") from e
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"The device file {path} isn't valid YAML: {e}") from e
    entries = doc.get("devices") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise ConfigError(f"The device file {path} needs a top-level 'devices:' list.")

    devices: list[DeviceConfig] = []
    problems: list[DeviceProblem] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        label = _entry_label(index, raw)
        if not isinstance(raw, dict):
            problems.append(DeviceProblem(index, None, label, f"{label}: each device must be a mapping of settings."))
            continue
        device, errs, good_name = _check_entry(label, raw, seen)
        if good_name is not None:
            seen.add(good_name)
        if device is not None:
            devices.append(device)
        else:
            problems.append(DeviceProblem(index, good_name, label, " ".join(errs)))
    return Devices(tuple(devices), tuple(problems))
