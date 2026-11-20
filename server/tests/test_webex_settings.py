"""Webex configuration reads no secrets or user vault data."""

from pathlib import Path

import pytest

from weaver.config import ConfigError, load_config

ROOTS = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")


def config_file(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("\n".join(f'{name} = "{name}"' for name in ROOTS) + "\n" + extra)
    return path


def test_full_pilot_settings_and_relative_paths(tmp_path: Path) -> None:
    path = config_file(
        tmp_path,
        """
[shepherds.webex]
enabled = true
instance_id = "mac-webex-primary"
auth = "integration_oauth"
credentials_file = "private/oauth.json"
courier_token_file = "private/capture.token"
exclude_space_ids = ["fake-space", "fake-other"]
poll_interval_seconds = 300
initial_backfill_days = 7
reconcile_overlap_days = 7
bundle = "space_day"
timezone = "Asia/Seoul"
attachments = "metadata_only"
""",
    )
    config = load_config(path, init_directories=True, environ={})
    settings = config.shepherds.webex
    assert settings.exclude_space_ids == ("fake-space", "fake-other")
    assert settings.credentials_file == tmp_path / "private/oauth.json"
    assert settings.courier_token_file == tmp_path / "private/capture.token"
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize(
    "field",
    [
        'client_secret = "fake-sensitive"',
        'access_token = "fake-sensitive"',
        'exclude_space_ids = "fake-sensitive"',
        "exclude_space_ids = [1]",
        "enabled = 1",
        "poll_interval_seconds = true",
        "initial_backfill_days = 0",
        'timezone = "fake-sensitive"',
        'auth = "fake-sensitive"',
        "credentials_file = 42",
        'credentials_file = "vault_root/oauth.json"',
        'credentials_file = "sync_root/oauth.json"',
    ],
)
def test_invalid_settings_do_not_leak_values(tmp_path: Path, field: str) -> None:
    path = config_file(tmp_path, "[shepherds.webex]\n" + field)
    with pytest.raises(ConfigError) as error:
        load_config(path, init_directories=True, environ={})
    assert "fake-sensitive" not in str(error.value)


def test_paths_must_be_distinct(tmp_path: Path) -> None:
    path = config_file(
        tmp_path,
        '[shepherds.webex]\ncredentials_file = "private/token"\ncourier_token_file = "private/token"',
    )
    with pytest.raises(ConfigError, match="distinct"):
        load_config(path, init_directories=True, environ={})


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    path = config_file(
        tmp_path, '[shepherds.webex]\ncredentials_file = "link/oauth.json"'
    )
    with pytest.raises(ConfigError, match="symbolic"):
        load_config(path, init_directories=True, environ={})


def test_enabled_requires_both_paths(tmp_path: Path) -> None:
    path = config_file(tmp_path, "[shepherds.webex]\nenabled = true")
    with pytest.raises(ConfigError, match="both"):
        load_config(path, init_directories=True, environ={})


def test_default_is_inert(tmp_path: Path) -> None:
    path = config_file(tmp_path, "")
    config = load_config(path, init_directories=True, environ={})
    assert config.shepherds.webex.exclude_space_ids == ()
    assert config.shepherds.webex.credentials_file is None
    assert not config.shepherds.webex.enabled
