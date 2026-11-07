"""Load one TOML file into immutable settings without selecting LLM clients.

No files or credentials are read on import. Data paths are relative to the
selected configuration file, and validation never prints supplied values.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

Backend = Literal["none", "anthropic", "agent_sdk", "claude_cli", "openai_compatible"]
_BACKENDS = {"none", "anthropic", "agent_sdk", "claude_cli", "openai_compatible"}
_ROOTS = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
_CHANNELS = ("outbox", "receipts", "proposals", "decisions")


class ConfigError(ValueError):
    """Invalid settings; messages contain field names, never supplied values."""


@dataclass(frozen=True)
class LlmConfig:
    """Shared Spinner/Loom input, containing a secret reference, never its value.

    ``none`` explicitly leaves the backend unselected. ``anthropic`` follows
    ADR-021; this module never constructs a client or initiates paid work.
    The future client resolves api_key_env just before calling the backend.
    """

    backend: Backend = "anthropic"
    api_key_env: str | None = None


@dataclass(frozen=True)
class ComponentConfig:
    """Settings to inject into either pipeline component."""

    llm: LlmConfig


@dataclass(frozen=True)
class CourierPaths:
    outbox: Path
    receipts: Path
    proposals: Path
    decisions: Path


@dataclass(frozen=True)
class ServerConfig:
    config_file: Path
    sync_root: Path
    archive_root: Path
    vault_root: Path
    state_root: Path
    log_root: Path
    courier: CourierPaths
    llm: LlmConfig

    @property
    def spinner(self) -> ComponentConfig:
        return ComponentConfig(self.llm)

    @property
    def loom(self) -> ComponentConfig:
        return ComponentConfig(self.llm)


def _path(value: str, base: Path, env: Mapping[str, str], field: str) -> Path:
    try:
        if value == "~" or value.startswith("~/"):
            home = env.get("HOME")
            if not home or not Path(home).is_absolute():
                raise ConfigError(f"{field}: home expansion requires absolute HOME")
            value = home + value[1:]
        elif value.startswith("~"):
            raise ConfigError(f"{field}: named-user home expansion is unsupported")
        if not value or "$" in value or "\x00" in value:
            raise ConfigError(f"{field}: expected a nonempty path without variables")
        path = Path(value)
        return (path if path.is_absolute() else base / path).resolve()
    except ConfigError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ConfigError(f"{field}: invalid path") from None


def _select_file(path: str | Path | None, env: Mapping[str, str]) -> Path:
    if path is not None:
        selected = str(path)
    elif "WEAVER_CONFIG" in env:
        # An explicit but empty selector is an error, never a silent fallback.
        selected = env["WEAVER_CONFIG"]
    elif env.get("XDG_CONFIG_HOME"):
        xdg = env["XDG_CONFIG_HOME"]
        if not Path(xdg).is_absolute():
            raise ConfigError("XDG_CONFIG_HOME: expected absolute path")
        selected = str(Path(xdg) / "weaver" / "config.toml")
    else:
        selected = "~/.config/weaver/config.toml"
    return _path(selected, Path.cwd(), env, "config_file")


def _llm_settings(value: object) -> LlmConfig:
    if not isinstance(value, dict):
        raise ConfigError("llm: expected a table")
    if value.keys() - {"backend", "api_key_env"}:
        raise ConfigError("llm: unknown fields (inline credentials are unsupported)")
    backend = value.get("backend", "anthropic")
    if not isinstance(backend, str) or backend not in _BACKENDS:
        raise ConfigError("llm.backend: unknown backend")
    reference = value.get("api_key_env")
    if "api_key_env" in value and (
        not isinstance(reference, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference) is None
    ):
        raise ConfigError("llm.api_key_env: expected environment variable name")
    return LlmConfig(cast(Backend, backend), cast(str | None, reference))


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise ConfigError("paths: cannot inspect directory identity") from None
    # This is the same identity comparison used by os.path.samefile().
    return info.st_dev, info.st_ino


def _overlaps(left: Path, right: Path) -> bool:
    if left.is_relative_to(right) or right.is_relative_to(left):
        return True
    # resolve() removes symlinks but retains case and macOS firmlink aliases.
    # Compare each root with the other's ancestors, not ancestors with each
    # other: disjoint siblings are allowed to share an existing parent.
    left_id, right_id = _identity(left), _identity(right)
    return (
        left_id is not None
        and any(left_id == _identity(parent) for parent in (right, *right.parents))
    ) or (
        right_id is not None
        and any(right_id == _identity(parent) for parent in (left, *left.parents))
    )


def _check_boundaries(roots: dict[str, Path]) -> None:
    # Public roots cannot contain each other, nor contain/be contained by private
    # storage. Private roots may share a parent or nest (e.g. logs inside state).
    for public in ("sync_root", "vault_root"):
        for other in _ROOTS:
            if other != public and _overlaps(roots[public], roots[other]):
                raise ConfigError(f"{public} and {other}: paths overlap")


def _check_directory(path: Path, field: str, allow_missing: bool) -> None:
    try:
        probe = path
        if not allow_missing and not probe.exists():
            raise ConfigError(f"{field}: directory must exist; initialize explicitly")
        while not probe.exists():
            probe = probe.parent
        if not probe.is_dir():
            raise ConfigError(f"{field}: expected directory")
        if not os.access(probe, os.W_OK | os.X_OK):
            raise ConfigError(f"{field}: directory must be writable and searchable")
    except OSError:
        raise ConfigError(f"{field}: cannot inspect directory") from None


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    init_directories: bool = False,
) -> ServerConfig:
    """Validate before use; only explicit initialization may create directories.

    Selection: argument > WEAVER_CONFIG > XDG_CONFIG_HOME/weaver/config.toml >
    ~/.config/weaver/config.toml. No file merging or per-field env overrides.
    All roots and Courier children must exist unless init_directories is true.
    Initialization creates directories only and never overwrites existing data.
    """
    env = os.environ if environ is None else environ
    config_file = _select_file(path, env)
    try:
        with config_file.open("rb") as stream:
            raw = tomllib.load(stream)
    except tomllib.TOMLDecodeError:
        raise ConfigError("config_file: invalid TOML") from None
    except (OSError, ValueError):
        raise ConfigError("config_file: cannot read configuration") from None
    if raw.keys() - {*_ROOTS, "llm"}:
        raise ConfigError("config_file: unknown fields")
    roots: dict[str, Path] = {}
    for field in _ROOTS:
        if field not in raw:
            raise ConfigError(f"{field}: missing required field")
        value = raw[field]
        if not isinstance(value, str):
            raise ConfigError(f"{field}: expected path string")
        roots[field] = _path(value, config_file.parent, env, field)
    llm = _llm_settings(raw.get("llm", {}))
    _check_boundaries(roots)
    channels: dict[str, Path] = {}
    for name in _CHANNELS:
        expected = roots["sync_root"] / name
        resolved = _path(str(expected), config_file.parent, env, "courier")
        if resolved != expected:
            raise ConfigError("courier: child directory must not redirect elsewhere")
        channels[name] = resolved
    directories = {**roots, **channels}
    # Check every path and nearest existing ancestor before the first mkdir.
    for field, directory in directories.items():
        _check_directory(directory, field, init_directories)
    if init_directories:
        for field, directory in directories.items():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ConfigError(f"{field}: cannot create directory") from None
            _check_directory(directory, field, False)
            # Newly created roots can reveal case/firmlink aliases that were
            # unobservable when both paths were absent. Reject before use.
            _check_boundaries(roots)
    return ServerConfig(
        config_file=config_file,
        sync_root=roots["sync_root"],
        archive_root=roots["archive_root"],
        vault_root=roots["vault_root"],
        state_root=roots["state_root"],
        log_root=roots["log_root"],
        courier=CourierPaths(**channels),
        llm=llm,
    )
