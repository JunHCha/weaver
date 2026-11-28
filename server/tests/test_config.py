"""Configuration uses temporary directories and fake environments only."""

import os
from pathlib import Path

import pytest

from weaver.config import (
    ConfigError,
    ReferenceVault,
    load_config,
    reference_clone_dir,
)

ROOTS = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
CHANNELS = ("outbox", "receipts", "proposals", "decisions")


def write_config(tmp_path: Path, extra: str = "", *, create: bool = True) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("\n".join(f'{key} = "{key}"' for key in ROOTS) + "\n" + extra)
    if create:
        for key in ROOTS:
            (tmp_path / key).mkdir(exist_ok=True)
        for channel in CHANNELS:
            (tmp_path / "sync_root" / channel).mkdir(exist_ok=True)
    return config


def test_normalized_shared_settings(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    config = load_config(path, environ={})
    assert config.sync_root == (tmp_path / "sync_root").resolve()
    assert config.courier.outbox == config.sync_root / "outbox"
    assert config.courier.receipts == config.sync_root / "receipts"
    assert config.courier.proposals == config.sync_root / "proposals"
    assert config.courier.decisions == config.sync_root / "decisions"
    assert config.llm.backend == "anthropic"
    assert config.spinner.llm is config.llm


@pytest.mark.parametrize(
    "backend", ["none", "anthropic", "agent_sdk", "claude_cli", "openai_compatible"]
)
def test_backends_do_not_read_credentials(tmp_path: Path, backend: str) -> None:
    path = write_config(
        tmp_path, f'[llm]\nbackend = "{backend}"\napi_key_env = "FAKE_KEY"\n'
    )
    config = load_config(path, environ={"FAKE_KEY": "sensitive-value"})
    assert config.llm.backend == backend
    assert config.llm.api_key_env == "FAKE_KEY"
    assert "sensitive-value" not in repr(config)


@pytest.mark.parametrize(
    "replacement",
    [
        "sync_root = 1",
        "sync_root = true",
        "sync_root = []",
        'sync_root = ""',
        'sync_root = "$HOME/data"',
    ],
)
def test_bad_path_types(tmp_path: Path, replacement: str) -> None:
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace('sync_root = "sync_root"', replacement))
    with pytest.raises(ConfigError, match="sync_root"):
        load_config(path, environ={})


@pytest.mark.parametrize(
    "extra",
    [
        '[llm]\nbackend = "secret-unknown"',
        "[llm]\nbackend = 1",
        'llm = "secret-value"',
        '[llm]\napi_key = "secret-value"',
        '[llm]\napi_key_env = "secret-value!"',
        '[llm]\nextra = "secret-value"',
        'typo = "secret-value"',
    ],
)
def test_reject_invalid_settings_without_values(tmp_path: Path, extra: str) -> None:
    path = write_config(tmp_path, extra)
    with pytest.raises(ConfigError) as error:
        load_config(path, environ={})
    assert "secret-" not in str(error.value)
    assert error.value.__cause__ is None


def test_missing_field_and_parse_errors_are_safe(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text('sync_root = "secret-value"\n')
    with pytest.raises(ConfigError, match="missing"):
        load_config(path, environ={})
    path.write_text('api_key = "secret-value" invalid')
    with pytest.raises(ConfigError) as error:
        load_config(path, environ={})
    assert "secret-value" not in str(error.value)


def test_selection_priority(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    xdg = tmp_path / "xdg"
    (xdg / "weaver").mkdir(parents=True)
    (xdg / "weaver" / "config.toml").write_text("invalid")
    env = {"WEAVER_CONFIG": str(path), "XDG_CONFIG_HOME": str(xdg)}
    assert load_config(environ=env).config_file == path
    assert load_config(path, environ={"WEAVER_CONFIG": "/missing"}).config_file == path
    with pytest.raises(ConfigError, match="TOML"):
        load_config(environ={"XDG_CONFIG_HOME": str(xdg)})


def test_default_location_and_home_expansion(tmp_path: Path) -> None:
    folder = tmp_path / ".config" / "weaver"
    folder.mkdir(parents=True)
    path = write_config(folder)
    path.write_text(
        path.read_text().replace(
            'archive_root = "archive_root"', 'archive_root = "~/archive"'
        )
    )
    (tmp_path / "archive").mkdir()
    config = load_config(environ={"HOME": str(tmp_path)})
    assert config.archive_root == tmp_path / "archive"
    assert config.config_file == path


@pytest.mark.parametrize(
    "private", ["archive_root", "state_root", "log_root", "vault_root"]
)
@pytest.mark.parametrize("kind", ["nested", "ancestor", "alias", "home"])
def test_reject_overlap(tmp_path: Path, private: str, kind: str) -> None:
    path = write_config(tmp_path)
    if kind == "nested":
        target = tmp_path / "sync_root" / "nested"
        target.mkdir()
    elif kind == "ancestor":
        target = tmp_path
    elif kind == "alias":
        target = tmp_path / "alias"
        target.symlink_to(tmp_path / "sync_root", target_is_directory=True)
    else:
        target = tmp_path / "sync_root"
    value = "~/sync_root" if kind == "home" else str(target)
    path.write_text(
        path.read_text().replace(f'{private} = "{private}"', f'{private} = "{value}"')
    )
    with pytest.raises(ConfigError, match="overlap"):
        load_config(path, environ={"HOME": str(tmp_path)})


@pytest.mark.parametrize("private", ["archive_root", "state_root", "log_root"])
def test_private_roots_cannot_overlap_vault(tmp_path: Path, private: str) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text().replace(
            f'{private} = "{private}"', f'{private} = "vault_root"'
        )
    )
    with pytest.raises(ConfigError, match="overlap"):
        load_config(path, environ={})


def test_reject_courier_symlink_escape(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    outbox = tmp_path / "sync_root" / "outbox"
    outbox.rmdir()
    outbox.symlink_to(tmp_path / "archive_root", target_is_directory=True)
    with pytest.raises(ConfigError, match="courier"):
        load_config(path, environ={})


def test_missing_directories_require_explicit_initialization(tmp_path: Path) -> None:
    path = write_config(tmp_path, create=False)
    with pytest.raises(ConfigError, match="exist"):
        load_config(path, environ={})
    assert not (tmp_path / "sync_root").exists()
    config = load_config(path, environ={}, init_directories=True)
    marker = config.courier.outbox / "existing.txt"
    marker.write_text("preserve")
    load_config(path, environ={}, init_directories=True)
    assert marker.read_text() == "preserve"


def test_all_validation_precedes_creation(tmp_path: Path) -> None:
    path = write_config(tmp_path, create=False)
    path.write_text(
        path.read_text().replace(
            'archive_root = "archive_root"', 'archive_root = "sync_root/nested"'
        )
    )
    with pytest.raises(ConfigError, match="overlap"):
        load_config(path, environ={}, init_directories=True)
    assert not (tmp_path / "sync_root").exists()


def test_file_instead_of_directory(tmp_path: Path) -> None:
    path = write_config(tmp_path, create=False)
    (tmp_path / "log_root").write_text("existing")
    with pytest.raises(ConfigError, match="directory"):
        load_config(path, environ={}, init_directories=True)
    assert not (tmp_path / "sync_root").exists()


def test_unwritable_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path)
    monkeypatch.setattr(os, "access", lambda *_args: False)
    with pytest.raises(ConfigError, match="writable"):
        load_config(path, environ={})


def test_missing_configuration(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="read"):
        load_config(tmp_path / "missing.toml", environ={})


def test_cwd_and_field_environment_do_not_change_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = load_config(path, environ={"WEAVER_SYNC_ROOT": "/ignored"})
    assert config.sync_root == tmp_path / "sync_root"


def test_cli_check_and_explicit_initialization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from weaver.app import main

    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_config(tmp_path, create=False)
    with pytest.raises(SystemExit) as error:
        main(["run", "--config", str(path), "--check-config"])
    assert error.value.code == 78  # EX_CONFIG
    assert not (tmp_path / "sync_root").exists()
    assert main(["run", "--config", str(path), "--init-directories"]) == 0
    assert main(["run", "--config", str(path), "--check-config"]) == 0
    assert "valid" in capsys.readouterr().out


def test_example_configuration(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "config.example.toml"
    path = tmp_path / "config.toml"
    path.write_bytes(example.read_bytes())
    config = load_config(path, environ={}, init_directories=True)
    assert config.llm.backend == "anthropic"
    assert config.courier.decisions.is_dir()


def test_missing_courier_child_is_not_created_during_check(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    child = tmp_path / "sync_root" / "receipts"
    child.rmdir()
    with pytest.raises(ConfigError, match="exist"):
        load_config(path, environ={})
    assert not child.exists()


def test_unwritable_initialization_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path, create=False)
    monkeypatch.setattr(os, "access", lambda *_args: False)
    with pytest.raises(ConfigError, match="writable"):
        load_config(path, environ={}, init_directories=True)
    assert not (tmp_path / "sync_root").exists()


def test_invalid_encoding_is_redacted(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b'key = "secret-value"\xff')
    with pytest.raises(ConfigError) as error:
        load_config(path, environ={})
    assert "secret-value" not in str(error.value)


def test_reject_symlink_loop_without_leaking_value(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    (tmp_path / "secret-loop").symlink_to(tmp_path / "secret-loop")
    path.write_text(
        path.read_text().replace(
            'archive_root = "archive_root"', 'archive_root = "secret-loop"'
        )
    )
    with pytest.raises(ConfigError) as error:
        load_config(path, environ={})
    assert "secret-loop" not in str(error.value)


def test_permission_bits_are_checked_on_real_directory(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    root = tmp_path / "archive_root"
    root.chmod(0o500)
    try:
        if os.access(root, os.W_OK):
            pytest.skip("current user bypasses directory write permissions")
        with pytest.raises(ConfigError, match="writable"):
            load_config(path, environ={})
    finally:
        root.chmod(0o700)


def test_private_roots_may_nest(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text().replace(
            'log_root = "log_root"', 'log_root = "state_root/logs"'
        )
    )
    config = load_config(path, environ={}, init_directories=True)
    assert config.log_root.parent == config.state_root


def test_relative_xdg_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="absolute"):
        load_config(environ={"HOME": str(tmp_path), "XDG_CONFIG_HOME": "relative"})


_BOUNDARY_PAIRS = [
    (public, other)
    for public in ("sync_root", "vault_root")
    for other in ROOTS
    if other != public
]


@pytest.mark.parametrize(("public", "other"), _BOUNDARY_PAIRS)
@pytest.mark.parametrize("preexisting", [True, False])
def test_case_insensitive_boundary_aliases(
    tmp_path: Path, public: str, other: str, preexisting: bool
) -> None:
    probe = tmp_path / "case-probe"
    probe.mkdir()
    alias = tmp_path / "CASE-PROBE"
    if not alias.exists() or not alias.samefile(probe):
        pytest.skip("requires a case-insensitive filesystem")
    path = write_config(tmp_path, create=False)
    root = tmp_path / "protected"
    if preexisting:
        root.mkdir()
    path.write_text(
        path.read_text()
        .replace(f'{public} = "{public}"', f'{public} = "protected"')
        .replace(f'{other} = "{other}"', f'{other} = "Protected/nested"')
    )
    with pytest.raises(ConfigError, match="overlap"):
        load_config(path, environ={}, init_directories=True)
    if preexisting:
        assert not (root / "nested").exists()


@pytest.mark.parametrize(("public", "other"), _BOUNDARY_PAIRS)
def test_firmlink_boundary_aliases(tmp_path: Path, public: str, other: str) -> None:
    alias = Path("/System/Volumes/Data") / str(tmp_path.resolve()).lstrip("/")
    if not alias.exists() or not alias.samefile(tmp_path):
        pytest.skip("requires the macOS data-volume firmlink")
    path = write_config(tmp_path)
    target = alias / public / "nested"
    path.write_text(
        path.read_text().replace(f'{other} = "{other}"', f'{other} = "{target}"')
    )
    with pytest.raises(ConfigError, match="overlap"):
        load_config(path, environ={}, init_directories=True)
    assert not target.exists()


@pytest.mark.parametrize(
    ("value", "env", "message"),
    [
        ("~/private", {}, "home expansion requires absolute HOME"),
        ("~/private", {"HOME": "relative"}, "home expansion requires absolute HOME"),
        ("~bob/private", {}, "named-user home expansion is unsupported"),
        ("$HOME/private", {}, "expected a nonempty path without variables"),
    ],
)
def test_specific_path_errors_are_preserved(
    tmp_path: Path, value: str, env: dict[str, str], message: str
) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text().replace(
            'archive_root = "archive_root"', f'archive_root = "{value}"'
        )
    )
    with pytest.raises(ConfigError, match=message) as error:
        load_config(path, environ=env)
    assert value not in str(error.value)


def test_empty_config_environment_policies(tmp_path: Path) -> None:
    folder = tmp_path / ".config" / "weaver"
    folder.mkdir(parents=True)
    path = write_config(folder)
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": ""}
    assert load_config(environ=env).config_file == path
    with pytest.raises(ConfigError, match="config_file"):
        load_config(environ={**env, "WEAVER_CONFIG": ""})


def test_removed_stt_table_is_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, '[stt]\nmodel_path = "model"\n')
    with pytest.raises(ConfigError, match="config_file: unknown fields"):
        load_config(config, environ={})


def test_shared_time_settings_default_and_override(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), environ={})
    assert config.time.timezone == "Asia/Seoul"
    assert config.time.zone.key == "Asia/Seoul"
    config = load_config(
        write_config(tmp_path, '[time]\ntimezone = "America/New_York"'), environ={}
    )
    assert config.time.zone.key == "America/New_York"


@pytest.mark.parametrize(
    "settings",
    [
        'time = "secret-value"',
        "[time]\ntimezone = false",
        '[time]\ntimezone = ""',
        '[time]\ntimezone = "secret-value"',
        '[time]\ntimezone = "/secret-value"',
        '[time]\nunknown = "secret-value"',
    ],
)
def test_invalid_time_settings_redact_values(tmp_path: Path, settings: str) -> None:
    with pytest.raises(ConfigError, match="time") as error:
        load_config(write_config(tmp_path, settings), environ={})
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("zone", ["Asia", "Factory", "posixrules", "localtime"])
def test_timezone_rejects_directory_and_nonregional_keys(
    tmp_path: Path, zone: str
) -> None:
    with pytest.raises(
        ConfigError, match="^time.timezone: unknown IANA timezone$"
    ) as error:
        load_config(write_config(tmp_path, f'[time]\ntimezone = "{zone}"'), environ={})
    assert error.value.__cause__ is None


def test_timezone_oserror_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_key: str) -> None:
        raise OSError("private path must not appear")

    monkeypatch.setattr("weaver.config.ZoneInfo", fail)
    with pytest.raises(
        ConfigError, match="^time.timezone: unknown IANA timezone$"
    ) as error:
        load_config(write_config(tmp_path), environ={})
    assert "private" not in str(error.value)


def test_all_shared_config_tables_coexist(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            '[llm]\nbackend = "none"\n[time]\ntimezone = "America/New_York"\n',
        ),
        environ={},
    )
    assert config.llm.backend == "none"
    assert config.time.timezone == "America/New_York"


def test_spinner_retry_config_and_factory(tmp_path: Path) -> None:
    from weaver.spinner import Spinner

    path = write_config(
        tmp_path,
        "[spinner]\nmax_attempts=4\nbackoff_seconds=0.5\nmax_backoff_seconds=2\n",
    )
    config = load_config(path, environ={})
    assert config.spinner.retry.max_attempts == 4
    assert config.spinner.retry.backoff_seconds == 0.5
    assert config.spinner.retry.max_backoff_seconds == 2

    class NoReads:
        def locate(self, source_id):
            raise AssertionError("factory must not read payloads")

    spinner = Spinner.from_config(NoReads(), config, configuration_version="test-v1")
    assert spinner.path == config.state_root / "spinner.sqlite3"


@pytest.mark.parametrize(
    "extra",
    [
        'spinner="secret-value"',
        '[spinner]\nunknown="secret-value"',
        "[spinner]\nmax_attempts=true",
        "[spinner]\nbackoff_seconds=nan",
    ],
)
def test_invalid_spinner_config(tmp_path: Path, extra: str) -> None:
    path = write_config(tmp_path, extra)
    with pytest.raises(ConfigError) as caught:
        load_config(path, environ={})
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(
    "extra",
    [
        "[refiner]\nbefore = 1",
        "[loom]\nfeedback_examples = false",
        "[publish]\nenabled = true",
    ],
)
def test_removed_tables_are_rejected(tmp_path: Path, extra: str) -> None:
    with pytest.raises(ConfigError, match="unknown fields"):
        load_config(write_config(tmp_path, extra), environ={})


def test_claude_cli_settings(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        '[llm]\nbackend = "claude_cli"\nclaude_path = "/opt/claude"\n'
        "max_cli_calls_per_day = 7\n",
    )
    llm = load_config(path, environ={}).llm
    assert llm.claude_path == Path("/opt/claude") and llm.max_cli_calls_per_day == 7
    default = load_config(write_config(tmp_path), environ={}).llm
    assert default.max_cli_calls_per_day == 30 and default.claude_path is None


@pytest.mark.parametrize(
    "extra",
    [
        "max_cli_calls_per_day = 0",
        "max_cli_calls_per_day = true",
        'claude_path = "claude"',
    ],
)
def test_claude_cli_settings_rejected(tmp_path: Path, extra: str) -> None:
    path = write_config(tmp_path, f'[llm]\nbackend = "claude_cli"\n{extra}\n')
    with pytest.raises(ConfigError):
        load_config(path, environ={})


def test_build_runtime_wires_claude_cli(tmp_path: Path) -> None:
    from weaver.llm import ClaudeCliClient, create_client
    from weaver.runtime.assemble import build_runtime

    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    path = write_config(
        tmp_path, f'[llm]\nbackend = "claude_cli"\nclaude_path = "{fake}"\n'
    )
    config = load_config(path, environ={})
    runtime = build_runtime(config, environ={})
    assert not runtime.gate.available  # the fake binary reports no login
    assert runtime.gate.status.reason == "claude_not_logged_in"
    client = create_client(config.llm, state_root=config.state_root)
    assert isinstance(client, ClaudeCliClient)


def test_courier_retry_settings(tmp_path: Path) -> None:
    default = load_config(write_config(tmp_path), environ={}).courier_settings
    assert default.max_attempts == 5
    configured = load_config(
        write_config(
            tmp_path,
            "[courier]\nmax_attempts = 2\nbackoff_seconds = 5\nmax_backoff_seconds = 10",
        ),
        environ={},
    ).courier_settings
    assert (
        configured.max_attempts,
        configured.backoff_seconds,
        configured.max_backoff_seconds,
    ) == (2, 5, 10)


@pytest.mark.parametrize(
    "settings", ["courier = 1", "[courier]\nunknown = 1", "[courier]\nmax_attempts = 0"]
)
def test_invalid_courier_settings(tmp_path: Path, settings: str) -> None:
    with pytest.raises(ConfigError, match="courier"):
        load_config(write_config(tmp_path, settings), environ={})


def test_codex_settings_and_invocation_override(tmp_path):
    from weaver.app import LlmBackend, _load

    path = write_config(
        tmp_path,
        """[llm]
backend = "codex_cli"
codex_path = "/opt/bin/codex"
codex_model = "fake-model"
codex_effort = "high"
max_cli_calls_per_day = 8
""",
    )
    settings = load_config(path, environ={})
    assert settings.llm.codex_model == "fake-model"
    assert settings.llm.codex_effort == "high"
    assert settings.llm.codex_path == Path("/opt/bin/codex")
    overridden = _load(str(path), llm=LlmBackend.claude_cli)
    assert overridden.llm.backend == "claude_cli"
    assert overridden.spinner.llm.backend == "claude_cli"
    assert load_config(path, environ={}).llm.backend == "codex_cli"


@pytest.mark.parametrize(
    "setting",
    [
        'codex_path = "relative"',
        'codex_model = ""',
        "codex_model = 1",
        'codex_effort = "secret-invalid"',
        "codex_effort = []",
    ],
)
def test_invalid_codex_settings(tmp_path, setting):
    path = write_config(tmp_path, "[llm]\n" + setting)
    with pytest.raises(ConfigError, match="llm.codex") as error:
        load_config(path, environ={})
    assert "secret-invalid" not in str(error.value)


def test_llm_option_with_config_check_and_initialization(tmp_path):
    from typer.testing import CliRunner

    from weaver.app import cli

    path = write_config(tmp_path, '[llm]\nbackend = "none"', create=False)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--config", str(path), "--llm", "codex_cli", "--init-directories"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli, ["run", "--config", str(path), "--llm", "claude_cli", "--check-config"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        cli, ["run", "--config", str(path), "--llm", "bad", "--check-config"]
    )
    assert result.exit_code == 2


def test_cli_timeouts_load_defaults_and_overrides(tmp_path):
    defaults = load_config(write_config(tmp_path), environ={}).llm
    assert defaults.cli_timeout_seconds == 300
    assert defaults.wiki_cli_timeout_seconds == 600
    settings = load_config(
        write_config(
            tmp_path,
            "[llm]\ncli_timeout_seconds = 450\nwiki_cli_timeout_seconds = 900\n",
        ),
        environ={},
    ).llm
    assert settings.cli_timeout_seconds == 450
    assert settings.wiki_cli_timeout_seconds == 900


@pytest.mark.parametrize("field", ["cli_timeout_seconds", "wiki_cli_timeout_seconds"])
@pytest.mark.parametrize("value", ["true", "0", "-1", "901", "nan", "inf", '"secret"'])
def test_cli_timeouts_reject_invalid_toml(tmp_path, field, value):
    with pytest.raises(ConfigError, match=field):
        load_config(write_config(tmp_path, f"[llm]\n{field} = {value}\n"), environ={})


@pytest.mark.parametrize("initialize", [False, True])
def test_http_config_does_not_require_or_initialize_shared_folders(
    tmp_path, initialize
):
    import shutil

    path = write_config(
        tmp_path, '[mobile]\ntransport = "http"\n[mobile.features]\nalert = "http"\n'
    )
    shutil.rmtree(tmp_path / "sync_root")
    config = load_config(path, environ={}, init_directories=initialize)
    assert not config.sync_root.exists()


def test_http_config_accepts_unwritable_shared_folder(tmp_path, monkeypatch):
    path = write_config(
        tmp_path, '[mobile]\ntransport = "http"\n[mobile.features]\nalert = "http"\n'
    )
    access = os.access
    sync = tmp_path / "sync_root"
    monkeypatch.setattr(
        os,
        "access",
        lambda p, mode: False if p == sync or sync in p.parents else access(p, mode),
    )
    assert load_config(path, environ={}).sync_root == sync.resolve()


def test_unset_pipeline_does_not_turn_all_http_back_into_files(tmp_path):
    path = write_config(
        tmp_path, '[mobile]\ntransport = "http"\n[mobile.features]\nalert = "http"\n'
    )
    mobile = load_config(path, environ={}).mobile
    assert not mobile.pipeline_explicit()
    assert mobile.transport_for("pipeline") == "files"  # feed stays off
    assert not mobile.uses_files()
    assert not mobile.publishes_pipeline_files()


def test_unset_pipeline_keeps_the_projection_rule_while_a_feature_uses_files(
    tmp_path,
):
    path = write_config(tmp_path, '[mobile.features]\ncapture = "http"\n')
    mobile = load_config(path, environ={}).mobile
    assert mobile.uses_files()
    assert mobile.publishes_pipeline_files()


@pytest.mark.parametrize("choice, files", [("http", False), ("files", True)])
def test_explicit_pipeline_overrides_the_projection_rule(tmp_path, choice, files):
    path = write_config(
        tmp_path,
        '[mobile]\ntransport = "http"\n[mobile.features]\n'
        f'alert = "http"\npipeline = "{choice}"\n',
    )
    mobile = load_config(path, environ={}).mobile
    assert mobile.pipeline_explicit()
    assert mobile.publishes_pipeline_files() is files
    assert mobile.uses_files() is files
    assert (
        load_config(
            write_config(
                tmp_path,
                '[mobile]\ntransport = "files"\n[mobile.features]\npipeline = "http"\n',
            ),
            environ={},
        ).mobile.publishes_pipeline_files()
        is False
    )


REFERENCE_TOML = """
[reference]
default_sphere = "회사"
[[reference.vault]]
sphere = "회사"
source = "git"
git_remote = "file:///x"
branch = "main"
[[reference.vault]]
sphere = "가족"
source = "home"
include = ["2. Area/가족"]
"""


def test_reference_vaults_parse(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, REFERENCE_TOML), environ={})
    assert config.reference.vaults[0] == ReferenceVault(
        "회사", "git", "file:///x", "main", ()
    )
    assert config.reference.vaults[1] == ReferenceVault(
        "가족", "home", None, "main", ("2. Area/가족",)
    )
    assert config.reference.default_sphere == "회사"
    assert load_config(write_config(tmp_path), environ={}).reference.vaults == ()


def _vault(*lines: str) -> str:
    return "[[reference.vault]]\n" + "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "extra",
    [
        _vault('sphere = "a"', 'source = "git"', 'git_remote = "x"', 'path = "/p"'),
        _vault('sphere = "a"', 'source = "git"'),
        _vault('sphere = "a"', 'source = "home"', 'git_remote = "x"'),
        _vault('sphere = "회사/x"', 'source = "home"'),
        _vault('sphere = ""', 'source = "home"'),
        _vault('sphere = "a"', 'source = "git"', 'git_remote = "x"')
        + _vault('sphere = "a"', 'source = "git"', 'git_remote = "y"'),
        _vault('sphere = "a"', 'source = "git"', 'git_remote = "x"')
        + _vault('sphere = "a"', 'source = "home"'),
        _vault('sphere = "a"', 'source = "home"')
        + _vault('sphere = "a"', 'source = "home"', 'include = ["d"]'),
        _vault('sphere = "a"', 'source = "git"', 'git_remote = "x"', 'include = ["d"]'),
        _vault('sphere = "a"', 'source = "ftp"'),
        '[reference]\ndefault_sphere = "없음"\n'
        + _vault('sphere = "a"', 'source = "home"'),
    ],
)
def test_reference_rejects_path_and_bad_combinations(
    tmp_path: Path, extra: str
) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, extra), environ={})


def test_reference_clone_dir_is_stable_per_sphere(tmp_path: Path) -> None:
    first = reference_clone_dir(tmp_path, "회사")
    assert first == reference_clone_dir(tmp_path, "회사")
    assert first != reference_clone_dir(tmp_path, "가족")
    assert first.relative_to(tmp_path).parts[0] == "reference"
    assert first.name == "repo"


def test_cli_json_reports_field_without_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from weaver.app import main

    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_config(tmp_path, create=False)
    with pytest.raises(SystemExit) as error:
        main(["run", "--config", str(path), "--check-config", "--json"])
    assert error.value.code == 78
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["ok"] is False
    assert report["field"] in ROOTS
    assert report["error"] == f"{report['field']}: {report['reason']}"
    assert str(tmp_path) not in captured.out
    assert captured.err == ""
    assert main(["run", "--config", str(path), "--init-directories", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize(
    ("env", "available", "reason"),
    [({}, False, "api_key_missing"), ({"WEAVER_TEST_KEY": "k"}, True, "configured")],
)
def test_cli_check_llm_reports_key_state_without_calling(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    available: bool,
    reason: str,
) -> None:
    import json

    from weaver.app import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WEAVER_TEST_KEY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = write_config(
        tmp_path, '[llm]\nbackend = "anthropic"\napi_key_env = "WEAVER_TEST_KEY"\n'
    )
    args = ["run", "--config", str(path), "--check-config", "--check-llm"]
    assert main([*args, "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "ok": True,
        "llm": {"backend": "anthropic", "available": available, "reason": reason},
    }
    assert '"k"' not in out
    assert main(args) == 0
    text = capsys.readouterr().out
    assert "Configuration valid." in text
    assert reason in text


@pytest.mark.parametrize("flag", ["--json", "--check-llm"])
def test_cli_report_flags_require_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    from weaver.app import main

    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_config(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(["run", "--config", str(path), flag])
    assert error.value.code == 2
    assert not (tmp_path / "log_root" / "weaver.log").exists()
