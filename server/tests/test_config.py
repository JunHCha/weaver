"""Configuration uses temporary directories and fake environments only."""

import os
from pathlib import Path

import pytest

from weaver.config import ConfigError, load_config

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
    assert config.spinner.llm is config.loom.llm is config.llm


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
        main(["--config", str(path), "--check-config"])
    assert error.value.code == 2
    assert not (tmp_path / "sync_root").exists()
    assert main(["--config", str(path), "--init-directories"]) == 0
    assert main(["--config", str(path), "--check-config"]) == 0
    assert "valid" in capsys.readouterr().out


def test_cli_runtime_remains_unimplemented(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from weaver.app import main

    path = write_config(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(["--config", str(path)])
    assert error.value.code == 2
    assert "not implemented" in capsys.readouterr().err


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


def test_stt_paths_and_spinner_settings(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), environ={})
    assert config.stt.model_path == config.state_root / "models/whisper-large-v3-turbo"
    assert config.spinner.stt is config.stt
    assert config.loom.stt is None
    config = load_config(
        write_config(tmp_path, '[stt]\nmodel_path = "models/turbo"\n'), environ={}
    )
    assert config.stt.model_path == tmp_path / "models/turbo"
    assert not config.stt.model_path.exists()


@pytest.mark.parametrize(
    "settings",
    [
        "stt = 3",
        "[stt]\nmodel_path = false",
        '[stt]\nmodel_path = ""',
        '[stt]\nengine = "other"',
    ],
)
def test_invalid_stt_settings(tmp_path: Path, settings: str) -> None:
    with pytest.raises(ConfigError, match="stt"):
        load_config(write_config(tmp_path, settings), environ={})
