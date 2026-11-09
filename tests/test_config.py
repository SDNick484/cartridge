from pathlib import Path

import pytest

from cartridge.config import load_devices, load_settings, parse_bind
from cartridge.errors import ApiError, ConfigError

EXAMPLE = """
devices:
  - name: family-room
    label: Family Room
    model: SuperStation One
    platform: mister
    transport: zaparoo
    host: 192.0.2.10
    port: 7497
    auth: legacy
  - name: office
    label: Office
    model: MiSTer Pi
    platform: mister
    transport: zaparoo
    host: 192.0.2.11
"""

MIXED = """
devices:
  - {name: office, host: 192.0.2.11}
  - {name: Bad Name!, host: 192.0.2.12}
  - {name: office, host: 192.0.2.13}
  - {name: portly, host: 192.0.2.14, port: 70000}
  - {name: hosty, host: "not a host!"}
  - {name: cabinet, host: 192.0.2.15, platform: batocera}
  - {name: pairy, host: 192.0.2.16, auth: paired}
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "devices.yaml"
    p.write_text(text)
    return p


def test_valid_devices_load(tmp_path: Path) -> None:
    result = load_devices(write(tmp_path, EXAMPLE))
    assert result.problems == ()
    fr, office = result.devices
    assert (fr.name, fr.label, fr.model) == ("family-room", "Family Room", "SuperStation One")
    assert (fr.host, fr.port) == ("192.0.2.10", 7497)
    assert (office.port, office.auth, office.platform, office.transport) == (7497, "legacy", "mister", "zaparoo")
    assert fr.strip_prefixes == ()


def test_label_and_model_defaults(tmp_path: Path) -> None:
    (dev,) = load_devices(write(tmp_path, "devices:\n  - {name: x, host: example.com}\n")).devices
    assert (dev.label, dev.model) == ("x", "")


def test_strip_prefixes(tmp_path: Path) -> None:
    text = "devices:\n  - {name: x, host: example.com, strip_prefixes: [/media/fat/games]}\n"
    (dev,) = load_devices(write(tmp_path, text)).devices
    assert dev.strip_prefixes == ("/media/fat/games",)
    bad = load_devices(write(tmp_path, "devices:\n  - {name: x, host: example.com, strip_prefixes: [games]}\n"))
    assert bad.devices == ()
    assert "strip_prefixes" in bad.problems[0].sentence


def test_bad_entry_does_not_block_others(tmp_path: Path) -> None:
    result = load_devices(write(tmp_path, MIXED))
    assert [d.name for d in result.devices] == ["office"]
    assert len(result.problems) == 6
    text = " ".join(p.sentence for p in result.problems)
    for needle in (
        "[a-z0-9-]",
        "already used",
        "1–65535",
        "batocera isn't supported in v1",
        "auth: paired isn't implemented yet",
    ):
        assert needle in text
    by_index = {p.index: p for p in result.problems}
    assert by_index[1].label == "Bad Name!"
    assert by_index[2].label == "office"
    assert all(p.label in p.sentence for p in result.problems)


def test_problem_keeps_name_when_valid(tmp_path: Path) -> None:
    by_index = {p.index: p for p in load_devices(write(tmp_path, MIXED)).problems}
    assert by_index[5].name == "cabinet"
    assert by_index[1].name is None
    assert by_index[2].name is None  # a duplicate cannot get its own card


def test_non_mapping_entry_is_a_problem(tmp_path: Path) -> None:
    result = load_devices(write(tmp_path, "devices:\n  - nope\n"))
    assert result.devices == ()
    assert result.problems[0].label == "entry #1"
    assert result.problems[0].name is None


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="devices.yaml"):
        load_devices(tmp_path / "devices.yaml")


def test_not_yaml_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_devices(write(tmp_path, "devices: [unclosed"))


def test_not_a_devices_list_is_config_error(tmp_path: Path) -> None:
    for text in ("- a\n- b\n", "devices: nope\n", "other: 1\n", ""):
        with pytest.raises(ConfigError, match="devices"):
            load_devices(write(tmp_path, text))


def test_settings_defaults() -> None:
    s = load_settings({"REMOTE_ENABLED": "0"})
    assert s.lan_bind == ("0.0.0.0", 8770)
    assert s.remote_bind == ("127.0.0.1", 8771)
    assert s.data_dir == Path("/var/lib/cartridge")
    assert s.devices_file == Path("/etc/cartridge/devices.yaml")
    assert s.art_online is True
    assert s.sync_interval_hours == 6
    assert s.remote_enabled is False
    assert s.lan_hosts is None
    assert s.log_level == "INFO"


def test_remote_enabled_without_access_refuses() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings({})
    assert "CF_ACCESS_TEAM_DOMAIN" in str(exc.value)
    assert "REMOTE_ENABLED=0" in str(exc.value)


def test_settings_parse() -> None:
    s = load_settings(
        {
            "LAN_HOSTS": "a.example.com, B.example.com",
            "CF_ACCESS_TEAM_DOMAIN": "team.example.com",
            "CF_ACCESS_AUD": "x,y",
            "REMOTE_ALLOWED_EMAILS": "A@Example.com, b@example.com",
            "LAN_BIND": "[::]:9000",
            "ART_ONLINE": "false",
            "SYNC_INTERVAL_HOURS": "0.5",
        }
    )
    assert s.lan_hosts == frozenset({"a.example.com", "b.example.com"})
    assert s.cf_access_aud == ("x", "y")
    assert s.cf_access_team_domain == "team.example.com"
    assert s.remote_allowed_emails == frozenset({"a@example.com", "b@example.com"})
    assert s.lan_bind == ("::", 9000)
    assert s.art_online is False
    assert s.sync_interval_hours == 0.5


def test_settings_errors_list_every_problem() -> None:
    env = {"REMOTE_ENABLED": "maybe", "SYNC_INTERVAL_HOURS": "abc", "LAN_BIND": "host:99999", "ART_ONLINE": "2"}
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    for name in ("REMOTE_ENABLED", "SYNC_INTERVAL_HOURS", "LAN_BIND", "ART_ONLINE"):
        assert name in str(exc.value)


def test_parse_bind() -> None:
    assert parse_bind("127.0.0.1:1234", 1) == ("127.0.0.1", 1234)
    assert parse_bind("[::1]:80", 1) == ("::1", 80)
    assert parse_bind("example.com", 8770) == ("example.com", 8770)
    assert parse_bind("::", 8770) == ("::", 8770)
    with pytest.raises(ConfigError):
        parse_bind("host:0", 1)


def test_api_error_fields() -> None:
    e = ApiError(429, "rate_limited", "Slow down.", extra={"k": 1}, retry_after=3)
    assert (e.status, e.code, e.message, e.extra, e.retry_after) == (429, "rate_limited", "Slow down.", {"k": 1}, 3)
    assert str(e) == "Slow down."


def test_trailing_newline_rejected_in_name_and_host(tmp_path: Path) -> None:
    text = 'devices:\n  - {name: "office\\n", host: example.com}\n  - {name: ok, host: "example.com\\n"}\n'
    result = load_devices(write(tmp_path, text))
    assert result.devices == ()
    assert len(result.problems) == 2


def test_non_utf8_file_is_config_error(tmp_path: Path) -> None:
    p = tmp_path / "devices.yaml"
    p.write_bytes("devices:\n  - {name: x, host: example.com, label: Sal\u00f3n}\n".encode("latin-1"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_devices(p)


@pytest.mark.parametrize("blank", ["", "null", "~", '""', "'   '"])
def test_blank_label_and_model_fall_back(tmp_path: Path, blank: str) -> None:
    text = f"devices:\n  - {{name: x, host: example.com, label: {blank}, model: {blank}}}\n"
    (dev,) = load_devices(write(tmp_path, text)).devices
    assert (dev.label, dev.model) == ("x", "")
