"""Load one TOML file into immutable settings without selecting LLM clients.

No files or credentials are read on import. Data paths are relative to the
selected configuration file, and validation never prints supplied values.
"""

import ipaddress
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from weaver.webex.settings import ShepherdSettings, WebexSettings

Backend = Literal[
    "none", "anthropic", "agent_sdk", "claude_cli", "codex_cli", "openai_compatible"
]
_BACKENDS = {
    "none",
    "anthropic",
    "agent_sdk",
    "claude_cli",
    "codex_cli",
    "openai_compatible",
}
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
    # Private key file (0600, owned by the user); takes precedence over
    # api_key_env because launchd jobs do not inherit the shell environment.
    api_key_file: Path | None = None
    # claude_cli only: absolute path to the official binary (else found on PATH)
    # and the daily call cap that protects the subscription allowance.
    claude_path: Path | None = None
    max_cli_calls_per_day: int = 30
    codex_path: Path | None = None
    codex_model: str | None = None
    codex_effort: str | None = None
    cli_timeout_seconds: float = 300
    wiki_cli_timeout_seconds: float = 600

    def __post_init__(self) -> None:
        for name in ("cli_timeout_seconds", "wiki_cli_timeout_seconds"):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= 900
            ):
                raise ConfigError(f"llm.{name}: expected finite number > 0 and <= 900")


@dataclass(frozen=True)
class WebexPrivacy:
    llm_allowed: bool = True
    policy_confirmed_at: str = "2026-11-19"


@dataclass(frozen=True)
class PrivacyChannels:
    webex: WebexPrivacy = WebexPrivacy()


@dataclass(frozen=True)
class PrivacySettings:
    channels: PrivacyChannels = PrivacyChannels()


def _privacy_settings(value: object) -> PrivacySettings:
    if not isinstance(value, dict) or value.keys() - {"channels"}:
        raise ConfigError("privacy: expected channels table")
    channels = value.get("channels", {})
    if not isinstance(channels, dict) or channels.keys() - {"webex"}:
        raise ConfigError("privacy.channels: unknown channel")
    webex = channels.get("webex", {})
    if not isinstance(webex, dict) or webex.keys() - {
        "llm_allowed",
        "policy_confirmed_at",
    }:
        raise ConfigError("privacy.channels.webex: unknown fields")
    allowed = webex.get("llm_allowed", True)
    confirmed = webex.get("policy_confirmed_at", "2026-11-19")
    if type(allowed) is not bool:
        raise ConfigError("privacy.channels.webex.llm_allowed: expected boolean")
    if not isinstance(confirmed, str):
        raise ConfigError("privacy.channels.webex.policy_confirmed_at: expected date")
    try:
        from datetime import date

        if date.fromisoformat(confirmed).isoformat() != confirmed:
            raise ValueError()
    except ValueError:
        raise ConfigError(
            "privacy.channels.webex.policy_confirmed_at: expected date"
        ) from None
    return PrivacySettings(PrivacyChannels(WebexPrivacy(allowed, confirmed)))


@dataclass(frozen=True)
class TimeConfig:
    """Shared timezone for interpreting local source timestamps."""

    timezone: str = "Asia/Seoul"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class SttConfig:
    """Local prepared model; loading config never downloads or loads weights."""

    model_path: Path


@dataclass(frozen=True)
class SpinnerSettings:
    """Bounded retries for explicitly transient parser/reviser failures."""

    max_attempts: int = 3
    backoff_seconds: float = 0.25
    max_backoff_seconds: float = 4

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ConfigError("spinner.max_attempts: expected positive integer")
        for name in ("backoff_seconds", "max_backoff_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ConfigError(f"spinner.{name}: expected finite nonnegative number")
        if self.backoff_seconds > self.max_backoff_seconds:
            raise ConfigError("spinner: backoff exceeds maximum")


@dataclass(frozen=True)
class CourierSettings:
    """Bounded, process-local retries of the same Verdict and error."""

    max_attempts: int = 5
    backoff_seconds: float = 30
    max_backoff_seconds: float = 1800

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ConfigError("courier.max_attempts: expected positive integer")
        for name in ("backoff_seconds", "max_backoff_seconds"):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ConfigError(f"courier.{name}: expected finite positive number")
        if self.backoff_seconds > self.max_backoff_seconds:
            raise ConfigError("courier: backoff exceeds maximum")


Transport = Literal["files", "http"]
TRANSPORT_FEATURES = ("capture", "review", "action", "alert", "pipeline")
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _check_bind(address: str) -> str:
    """Only an explicit tailnet (or loopback, for tests) address; never wildcard."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ConfigError("mobile.bind: expected an IP address") from None
    if ip.is_unspecified:
        raise ConfigError("mobile.bind: wildcard addresses are not allowed")
    if not (
        ip.is_loopback
        or (ip.version == 4 and ip in _TAILNET_V4)
        or (ip.version == 6 and ip in _TAILNET_V6)
    ):
        raise ConfigError("mobile.bind: expected a tailnet or loopback address")
    return str(ip)


@dataclass(frozen=True)
class MobileSettings:
    """Phone-only HTTPS API (ADR-040). Disabled until ``bind`` is configured.

    ``transport`` picks the write path per feature: ``files`` (Syncthing folders,
    the default) or ``http``. ``features`` overrides it for single features.
    """

    bind: str | None = None
    port: int = 8787
    whois: Literal["localapi", "cli"] = "cli"
    whois_socket: Path = Path("/var/run/tailscaled.socket")
    tailscale_cli: Path | None = None
    transport: Transport = "files"
    features: tuple[tuple[str, Transport], ...] = ()
    max_payload_bytes: int = 64 * 1024 * 1024
    staging_days: int = 7
    # Count from the last receipt among all active review devices.
    alert_body_days: int | None = 30

    def __post_init__(self) -> None:
        if self.bind is not None:
            _check_bind(self.bind)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ConfigError("mobile.port: expected 1-65535")
        if self.transport not in ("files", "http"):
            raise ConfigError("mobile.transport: expected files or http")
        if type(self.max_payload_bytes) is not int or self.max_payload_bytes < 1:
            raise ConfigError("mobile.max_payload_bytes: expected positive integer")
        if type(self.staging_days) is not int or self.staging_days < 1:
            raise ConfigError("mobile.staging_days: expected positive integer")

        if self.alert_body_days is not None and (
            type(self.alert_body_days) is not int or self.alert_body_days < 1
        ):
            raise ConfigError("mobile.alert_body_days: expected positive integer")

    def transport_for(self, feature: str) -> Transport:
        # Alert and pipeline HTTP need the phone's new ledger/read client. Existing
        # global HTTP installs keep their files until the APK rollout.
        return dict(self.features).get(
            feature,
            "files" if feature in ("alert", "pipeline") else self.transport,
        )

    def uses_files(self) -> bool:
        """Shared Courier projections are needed by any file transport user."""
        return any(
            self.transport_for(feature) == "files" for feature in TRANSPORT_FEATURES
        )


def _mobile_settings(
    value: object, env: Mapping[str, str], base: Path
) -> MobileSettings:
    if not isinstance(value, dict):
        raise ConfigError("mobile: expected a table")
    if value.keys() - {
        "bind",
        "port",
        "whois",
        "whois_socket",
        "tailscale_cli",
        "transport",
        "features",
        "max_payload_bytes",
        "staging_days",
        "alert_body_days",
    }:
        raise ConfigError("mobile: unknown fields")
    values: dict[str, object] = {}
    for key in (
        "bind",
        "port",
        "whois",
        "transport",
        "max_payload_bytes",
        "staging_days",
        "alert_body_days",
    ):
        if key in value:
            values[key] = value[key]
    if "bind" in values and not isinstance(values["bind"], str):
        raise ConfigError("mobile.bind: expected string")
    if values.get("whois", "cli") not in ("localapi", "cli"):
        raise ConfigError("mobile.whois: expected localapi or cli")
    for key in ("whois_socket", "tailscale_cli"):
        if key in value:
            raw = value[key]
            if not isinstance(raw, str):
                raise ConfigError(f"mobile.{key}: expected path string")
            values[key] = _path(raw, base, env, f"mobile.{key}")
    features = value.get("features", {})
    if not isinstance(features, dict) or features.keys() - set(TRANSPORT_FEATURES):
        raise ConfigError("mobile.features: unknown feature")
    if any(choice not in ("files", "http") for choice in features.values()):
        raise ConfigError("mobile.features: expected files or http")
    values["features"] = tuple(sorted(features.items()))
    return MobileSettings(**values)  # type: ignore[arg-type]


def _check_folder(enabled: object, folder: object) -> None:
    if type(enabled) is not bool:
        raise ConfigError("wiki.enabled: expected boolean")
    if (
        not isinstance(folder, str)
        or not folder
        or "\\" in folder
        or "\0" in folder
        or any(
            part in ("", ".", "..") or part.startswith(".")
            for part in folder.split("/")
        )
    ):
        raise ConfigError("wiki.folder: expected relative folder name")


@dataclass(frozen=True)
class WikiSettings:
    """Settings for periodic wiki output."""

    enabled: bool = False
    vault_root: Path | None = None
    folder: str = "Weaver"
    timezone: str = "Asia/Seoul"
    settle_seconds: float = 1800
    interval_seconds: float = 60

    def __post_init__(self) -> None:
        _check_folder(self.enabled, self.folder)
        if (
            not isinstance(self.timezone, str)
            or self.timezone not in available_timezones()
            or self.timezone in {"Factory", "localtime", "posixrules"}
        ):
            raise ConfigError("wiki.timezone: unknown IANA timezone")
        for name in ("settle_seconds", "interval_seconds"):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                or (name == "interval_seconds" and value == 0)
            ):
                raise ConfigError(f"wiki.{name}: invalid interval")


@dataclass(frozen=True)
class ComponentConfig:
    """Pipeline settings; Loom must use ServerConfig.time for local timestamps."""

    llm: LlmConfig
    stt: SttConfig | None = None
    retry: SpinnerSettings = SpinnerSettings()


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
    stt: SttConfig
    time: TimeConfig = TimeConfig()
    spinner_settings: SpinnerSettings = SpinnerSettings()
    wiki: WikiSettings = dataclass_field(default_factory=WikiSettings)
    courier_settings: CourierSettings = CourierSettings()
    mobile: MobileSettings = dataclass_field(default_factory=MobileSettings)
    shepherds: ShepherdSettings = dataclass_field(default_factory=ShepherdSettings)
    privacy: PrivacySettings = PrivacySettings()

    @property
    def spinner(self) -> ComponentConfig:
        return ComponentConfig(self.llm, self.stt, retry=self.spinner_settings)


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


def _llm_settings(value: object, base: Path, env: Mapping[str, str]) -> LlmConfig:
    if not isinstance(value, dict):
        raise ConfigError("llm: expected a table")
    if value.keys() - {
        "backend",
        "api_key_env",
        "api_key_file",
        "claude_path",
        "codex_path",
        "codex_model",
        "codex_effort",
        "max_cli_calls_per_day",
        "cli_timeout_seconds",
        "wiki_cli_timeout_seconds",
    }:
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
    key_file: Path | None = None
    if "api_key_file" in value:
        raw_file = value["api_key_file"]
        if not isinstance(raw_file, str) or not (
            raw_file.startswith("/") or raw_file.startswith("~/")
        ):
            raise ConfigError("llm.api_key_file: expected absolute path")
        key_file = _path(raw_file, base, env, "llm.api_key_file")
    claude_path: Path | None = None
    if "claude_path" in value:
        raw_claude = value["claude_path"]
        if not isinstance(raw_claude, str) or not (
            raw_claude.startswith("/") or raw_claude.startswith("~/")
        ):
            raise ConfigError("llm.claude_path: expected absolute path")
        claude_path = _path(raw_claude, base, env, "llm.claude_path")
    codex_path: Path | None = None
    if "codex_path" in value:
        raw_codex = value["codex_path"]
        if not isinstance(raw_codex, str) or not (
            raw_codex.startswith("/") or raw_codex.startswith("~/")
        ):
            raise ConfigError("llm.codex_path: expected absolute path")
        codex_path = _path(raw_codex, base, env, "llm.codex_path")
    model = value.get("codex_model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError("llm.codex_model: expected nonempty string")
    effort = value.get("codex_effort")
    if effort is not None and effort not in (
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ):
        raise ConfigError("llm.codex_effort: unknown effort")
    cap = value.get("max_cli_calls_per_day", 30)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise ConfigError("llm.max_cli_calls_per_day: expected integer >= 1")
    return LlmConfig(
        cast(Backend, backend),
        cast(str | None, reference),
        key_file,
        claude_path,
        cap,
        codex_path,
        model,
        cast(str | None, effort),
        value.get("cli_timeout_seconds", 300),
        value.get("wiki_cli_timeout_seconds", 600),
    )


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
        for other in roots:
            if other != public and _overlaps(roots[public], roots[other]):
                raise ConfigError(f"{public} and {other}: paths overlap")


def _check_directory(
    path: Path, field: str, allow_missing: bool, *, require_writable: bool = True
) -> None:
    try:
        probe = path
        if not allow_missing and not probe.exists():
            raise ConfigError(f"{field}: directory must exist; initialize explicitly")
        while not probe.exists():
            probe = probe.parent
        if not probe.is_dir():
            raise ConfigError(f"{field}: expected directory")
        if require_writable and not os.access(probe, os.W_OK | os.X_OK):
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
    Private roots must exist unless init_directories is true. Courier shared
    folders are optional when every transport uses HTTP.
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
    if raw.keys() - {
        *_ROOTS,
        "llm",
        "stt",
        "time",
        "spinner",
        "courier",
        "wiki",
        "mobile",
        "shepherds",
        "privacy",
    }:
        raise ConfigError("config_file: unknown fields")
    roots: dict[str, Path] = {}
    for field in _ROOTS:
        if field not in raw:
            raise ConfigError(f"{field}: missing required field")
        value = raw[field]
        if not isinstance(value, str):
            raise ConfigError(f"{field}: expected path string")
        roots[field] = _path(value, config_file.parent, env, field)
    time_raw = raw.get("time", {})
    if not isinstance(time_raw, dict) or time_raw.keys() - {"timezone"}:
        raise ConfigError("time: expected a table containing only timezone")
    timezone = time_raw.get("timezone", "Asia/Seoul")
    if not isinstance(timezone, str) or not timezone:
        raise ConfigError("time.timezone: expected IANA timezone name")
    try:
        # Factory is tzdb's unspecified-timezone sentinel, not a real region.
        # localtime/posixrules depend on host configuration rather than a source.
        if timezone not in available_timezones() or timezone in {
            "Factory",
            "localtime",
            "posixrules",
        }:
            raise ValueError("unsupported timezone key")
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError("time.timezone: unknown IANA timezone") from None
    time_config = TimeConfig(timezone)
    spinner_raw = raw.get("spinner", {})
    if not isinstance(spinner_raw, dict) or spinner_raw.keys() - {
        "max_attempts",
        "backoff_seconds",
        "max_backoff_seconds",
    }:
        raise ConfigError("spinner: expected retry settings table")
    spinner_settings = SpinnerSettings(**spinner_raw)
    courier_raw = raw.get("courier", {})
    if not isinstance(courier_raw, dict) or courier_raw.keys() - {
        "max_attempts",
        "backoff_seconds",
        "max_backoff_seconds",
    }:
        raise ConfigError("courier: expected retry settings table")
    courier_settings = CourierSettings(**courier_raw)
    wiki_raw = raw.get("wiki", {})
    if not isinstance(wiki_raw, dict) or wiki_raw.keys() - {
        "enabled",
        "vault_root",
        "folder",
        "timezone",
        "settle_seconds",
        "interval_seconds",
    }:
        raise ConfigError("wiki: unknown fields")
    wiki_values = dict(wiki_raw)
    if "vault_root" in wiki_values:
        if not isinstance(wiki_values["vault_root"], str):
            raise ConfigError("wiki.vault_root: expected path string")
        wiki_values["vault_root"] = _path(
            wiki_values["vault_root"], config_file.parent, env, "wiki.vault_root"
        )
    wiki = WikiSettings(**wiki_values)
    shepherds_raw = raw.get("shepherds", {})
    if not isinstance(shepherds_raw, dict) or shepherds_raw.keys() - {
        "webex",
        "courier",
    }:
        raise ConfigError("shepherds: expected webex/courier tables")
    webex_raw = shepherds_raw.get("webex", {})
    if (
        not isinstance(webex_raw, dict)
        or webex_raw.keys() - WebexSettings.__dataclass_fields__.keys()
    ):
        raise ConfigError(
            "shepherds.webex: unknown fields (use secret file references)"
        )
    webex_values = dict(webex_raw)
    for name in ("credentials_file", "courier_token_file"):
        if name not in webex_values:
            continue
        value = webex_values[name]
        field = f"shepherds.webex.{name}"
        if not isinstance(value, str):
            raise ConfigError(f"{field}: expected path string")
        resolved = _path(value, config_file.parent, env, field)
        expanded = (
            value.replace("~", env.get("HOME", ""), 1)
            if value.startswith("~/")
            else value
        )
        lexical = Path(os.path.abspath(config_file.parent / expanded))
        if lexical != resolved:
            raise ConfigError(f"{field}: symbolic links are unsupported")
        if any(
            _overlaps(resolved.parent, root)
            for root in (
                roots["sync_root"],
                roots["vault_root"],
                *([wiki.vault_root] if wiki.vault_root is not None else []),
            )
        ):
            raise ConfigError(f"{field}: parent must be outside sync/vault")
        webex_values[name] = resolved
    if "exclude_space_ids" in webex_values:
        ids = webex_values["exclude_space_ids"]
        if not isinstance(ids, list):
            raise ConfigError(
                "shepherds.webex.exclude_space_ids: expected string array"
            )
        webex_values["exclude_space_ids"] = tuple(ids)
    from weaver.shepherd.settings import CourierSettings as ShepherdCourierSettings

    courier_raw = shepherds_raw.get("courier", {})
    if (
        not isinstance(courier_raw, dict)
        or courier_raw.keys() - ShepherdCourierSettings.__dataclass_fields__.keys()
    ):
        raise ConfigError("shepherds.courier: unknown fields")
    try:
        local_courier = ShepherdCourierSettings(**courier_raw)
    except (ValueError, TypeError) as error:
        raise ConfigError(str(error)) from None
    shepherds = ShepherdSettings(WebexSettings(**webex_values), local_courier)
    if (
        shepherds.webex.credentials_file is not None
        and shepherds.webex.credentials_file == shepherds.webex.courier_token_file
    ):
        raise ConfigError("shepherds.webex: secret files must be distinct")
    mobile = _mobile_settings(raw.get("mobile", {}), env, config_file.parent)
    if (
        mobile.bind in (local_courier.bind, "0.0.0.0", "::")
        and mobile.port == local_courier.port
    ):
        raise ConfigError("shepherds.courier: listener must be separate from mobile")
    if local_courier.port == 8765:
        raise ConfigError("shepherds.courier: port conflicts with default web listener")
    llm = _llm_settings(raw.get("llm", {}), config_file.parent, env)
    privacy = _privacy_settings(raw.get("privacy", {}))
    stt_raw = raw.get("stt", {})
    if not isinstance(stt_raw, dict) or stt_raw.keys() - {"model_path"}:
        raise ConfigError("stt: expected a table containing only model_path")
    model_path = stt_raw.get(
        "model_path", str(roots["state_root"] / "models" / "whisper-large-v3-turbo")
    )
    if not isinstance(model_path, str):
        raise ConfigError("stt.model_path: expected path string")
    stt = SttConfig(_path(model_path, config_file.parent, env, "stt.model_path"))
    boundaries = {**roots, "stt.model_path": stt.model_path}
    _check_boundaries(boundaries)
    if wiki.vault_root is not None:
        for field in ("sync_root", "archive_root", "state_root", "log_root"):
            if _overlaps(wiki.vault_root, roots[field]):
                raise ConfigError(f"wiki.vault_root and {field}: paths overlap")
        _check_directory(wiki.vault_root, "wiki.vault_root", init_directories)
        if init_directories:
            wiki.vault_root.mkdir(parents=True, exist_ok=True)
    if llm.api_key_file is not None and any(
        _overlaps(llm.api_key_file, roots[public])
        for public in ("sync_root", "vault_root")
    ):
        raise ConfigError("llm.api_key_file: must not be inside sync_root/vault_root")
    channels: dict[str, Path] = {}
    for name in _CHANNELS:
        expected = roots["sync_root"] / name
        resolved = _path(str(expected), config_file.parent, env, "courier")
        if resolved != expected:
            raise ConfigError("courier: child directory must not redirect elsewhere")
        channels[name] = resolved
    directories = {**roots, **channels}
    # HTTP does not initialize or require the rollback-only shared folders.
    shared_optional = not mobile.uses_files()
    optional = {"sync_root", *channels} if shared_optional else set()
    # Check every path and nearest existing ancestor before the first mkdir.
    for field, directory in directories.items():
        _check_directory(
            directory,
            field,
            init_directories or field in optional,
            require_writable=field not in optional,
        )
    if init_directories:
        for field, directory in directories.items():
            if field in optional:
                continue
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ConfigError(f"{field}: cannot create directory") from None
            _check_directory(directory, field, False)
            # Newly created roots can reveal case/firmlink aliases that were
            # unobservable when both paths were absent. Reject before use.
            _check_boundaries(boundaries)
    return ServerConfig(
        config_file=config_file,
        sync_root=roots["sync_root"],
        archive_root=roots["archive_root"],
        vault_root=roots["vault_root"],
        state_root=roots["state_root"],
        log_root=roots["log_root"],
        courier=CourierPaths(**channels),
        llm=llm,
        stt=stt,
        time=time_config,
        spinner_settings=spinner_settings,
        courier_settings=courier_settings,
        wiki=wiki,
        mobile=mobile,
        shepherds=shepherds,
        privacy=privacy,
    )
