"""Subscription adapter: the official ``claude -p`` binary as a subprocess.

Authentication belongs entirely to the CLI. This module never reads OAuth
tokens, keychain items or session files, and the child only receives an
allow-listed environment so an API key can never turn a subscription call into
a billed API call. Prompts travel over stdin, never argv. Response bodies and
stderr are parsed in memory and never logged or placed in exceptions.
"""

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import anthropic
from pydantic import BaseModel, ValidationError

from weaver.config import LlmConfig
from weaver.failures import failure_reason

from .errors import (
    AuthenticationError,
    InvalidJson,
    LlmError,
    LlmTimeout,
    MissingOutput,
    RateLimited,
    SchemaMismatch,
    UsageLimited,
)
from .ledger import Ledger
from .types import TokenUsage

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
ENV_ALLOWLIST = ("HOME", "PATH", "USER", "LANG", "LC_ALL", "TMPDIR")
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
WORKDIR_NAME = "claude-cli-workdir"
STATUS_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflow: bool = False


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        cwd: Path,
        timeout: float,
    ) -> RunResult: ...


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Only the allow-list; ANTHROPIC_*/CLAUDE_CODE_* must never reach the CLI."""
    return {name: environ[name] for name in ENV_ALLOWLIST if name in environ}


def find_claude(config: LlmConfig, environ: Mapping[str, str]) -> str | None:
    if config.claude_path is not None:
        path = str(config.claude_path)
        return path if os.access(path, os.X_OK) and os.path.isfile(path) else None
    return shutil.which("claude", path=environ.get("PATH", os.defpath))


def _drain(
    stream: Any, cap: int, sink: list[bytes], flag: threading.Event | None
) -> None:
    """Keep up to ``cap`` bytes. Past it: kill signal via ``flag``, or, without
    a flag, discard the excess but keep reading so the child never blocks."""
    total = 0
    try:
        while chunk := stream.read(65536):
            if total + len(chunk) > cap:
                if flag is not None:
                    flag.set()
                    return
                chunk = chunk[: cap - total]
                if not chunk:
                    continue
            total += len(chunk)
            sink.append(chunk)
    except (OSError, ValueError):
        return


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def subprocess_runner(
    argv: Sequence[str],
    *,
    stdin: bytes,
    env: Mapping[str, str],
    cwd: Path,
    timeout: float,
) -> RunResult:
    proc = subprocess.Popen(  # noqa: S603 - argv is built here, no shell
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,  # own process group so a timeout kills children
    )
    assert proc.stdin and proc.stdout and proc.stderr
    out: list[bytes] = []
    err: list[bytes] = []
    overflow = threading.Event()
    readers = [
        threading.Thread(
            target=_drain, args=(proc.stdout, MAX_OUTPUT_BYTES, out, overflow)
        ),
        threading.Thread(target=_drain, args=(proc.stderr, 65536, err, None)),
    ]

    def feed() -> None:
        try:
            proc.stdin.write(stdin)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass

    feeder = threading.Thread(target=feed)
    for thread in (*readers, feeder):
        thread.daemon = True
        thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while proc.poll() is None:
        if overflow.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)
    if proc.poll() is None or timed_out or overflow.is_set():
        _kill_group(proc)
    proc.wait()
    # Grandchildren holding the pipes were killed with the group.
    for thread in readers:
        thread.join(timeout=2)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.close()
    return RunResult(
        proc.returncode, b"".join(out), b"".join(err), timed_out, overflow.is_set()
    )


@dataclass(frozen=True)
class CliStatus:
    available: bool
    reason: str


_status_cache: dict[str, tuple[float, CliStatus]] = {}
_status_lock = threading.Lock()


def cli_status(
    config: LlmConfig,
    environ: Mapping[str, str],
    *,
    runner: Runner = subprocess_runner,
    ttl: float = STATUS_TTL_SECONDS,
    now: float | None = None,
) -> CliStatus:
    """Executable exists and ``claude auth status`` says logged in (cached)."""
    binary = find_claude(config, environ)
    if binary is None:
        return CliStatus(False, "claude_not_found")
    clock = time.monotonic() if now is None else now
    key = binary
    with _status_lock:
        hit = _status_cache.get(key)
        if hit is not None and clock - hit[0] < ttl:
            return hit[1]
    result = _probe(binary, environ, runner)
    with _status_lock:
        _status_cache[key] = (clock, result)
    return result


def _probe(binary: str, environ: Mapping[str, str], runner: Runner) -> CliStatus:
    try:
        res = runner(
            [binary, "auth", "status", "--json"],
            stdin=b"",
            env=child_env(environ),
            cwd=Path(os.getcwd()),
            timeout=15,
        )
    except OSError:
        return CliStatus(False, "claude_unavailable")
    if res.timed_out:
        return CliStatus(False, "claude_status_timeout")
    try:
        data = json.loads(res.stdout)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("loggedIn") is True:
        return CliStatus(True, "configured")
    return CliStatus(False, "claude_not_logged_in")


def _classify(text: str, status: object) -> LlmError:
    """Map a CLI failure to a stable code; ``text`` is inspected, never kept."""
    lowered = text.lower()
    if status in (401, 403) or re.search(
        r"not logged in|please (run )?/?login|invalid api key|authenticat|oauth",
        lowered,
    ):
        return AuthenticationError()
    if re.search(r"usage limit|limit reached|quota", lowered):
        return UsageLimited()
    if status == 429 or re.search(r"rate limit|too many requests", lowered):
        return RateLimited()
    return LlmError()


class ClaudeCliClient:
    backend = "claude_cli"
    workdir_name = WORKDIR_NAME
    model = MODEL  # Read by Loom version tracking; not part of LlmClient.

    def __init__(
        self,
        config: LlmConfig,
        ledger: Ledger,
        *,
        state_root: Path,
        runner: Runner = subprocess_runner,
        environ: Mapping[str, str] | None = None,
        deadline_seconds: float = 120,
    ) -> None:
        if config.backend != self.backend:
            raise ValueError("ClaudeCliClient requires claude_cli backend")
        if not 0 < deadline_seconds <= 600:
            raise ValueError("invalid deadline")
        self.config = config
        self.ledger = ledger
        self.runner = runner
        self.environ = os.environ if environ is None else environ
        self.deadline_seconds = deadline_seconds
        self.workdir = state_root / self.workdir_name

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        return cast(T, self._generate(task, system, user, schema))

    def generate_text(self, task: str, system: str, user: str) -> str:
        return cast(str, self._generate(task, system, user, None))

    def _argv(self, binary: str, system: str, json_schema: str | None) -> list[str]:
        argv = [
            binary,
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--model",
            MODEL,
            "--system-prompt",
            system,
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--disable-slash-commands",
        ]
        if json_schema is not None:
            argv += ["--json-schema", json_schema]
        return argv

    def _generate[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T] | None
    ) -> Any:
        if (
            re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", task) is None
            or len(task) > 80
        ):
            raise ValueError("task must be a short machine identifier")
        started = time.monotonic()
        record: dict[str, Any] = {
            "task": task,
            "backend": self.backend,
            "model": self.model,
            "daily_cap": self.config.max_cli_calls_per_day,
            "started_at": datetime.now(UTC).isoformat(),
            "status": "pending",
            "elapsed_seconds": 0.0,
            "attempts": 0,
            "usage": [],
            "cost_usd": None,
            "cost_source": "unavailable",
            "attempt_records": [],
        }
        # Subscription calls carry no dollar charge: the month budget gets 0.
        call_id = self.ledger.reserve(record, 0)
        try:
            result = self._run(record, started, system, user, schema)
            record["status"] = "succeeded"
            return result
        except LlmError as exc:
            if isinstance(exc, (RateLimited, UsageLimited)) and "daily_day" in record:
                self.ledger.refund_daily_call(record.pop("daily_day"))
            record["status"] = exc.code
            raise
        except Exception:
            record["status"] = LlmError.code
            raise LlmError() from None
        finally:
            record.pop("daily_day", None)
            record["elapsed_seconds"] = time.monotonic() - started
            self.ledger.finish(call_id, record, 0)

    def _run[T: BaseModel](
        self,
        record: dict[str, Any],
        started: float,
        system: str,
        user: str,
        schema: type[T] | None,
    ) -> Any:
        binary = find_claude(self.config, self.environ)
        if binary is None:
            raise AuthenticationError()
        json_schema: str | None = None
        if schema is not None:
            json_schema = json.dumps(anthropic.transform_schema(schema))
        day = datetime.now().astimezone().date().isoformat()
        if not self.ledger.take_daily_call(day, self.config.max_cli_calls_per_day):
            logger.warning("Claude CLI daily call cap reached; call refused")
            raise UsageLimited()
        record["daily_day"] = day
        self.workdir.mkdir(mode=0o700, exist_ok=True)
        record["attempts"] = 1
        attempt: dict[str, Any] = {"status": "pending", "usage": None}
        record["attempt_records"].append(attempt)
        try:
            res = self.runner(
                self._argv(binary, system, json_schema),
                stdin=user.encode("utf-8"),
                env=child_env(self.environ),
                cwd=self.workdir,
                timeout=self.deadline_seconds,
            )
        except OSError:
            attempt["status"] = LlmError.code
            raise LlmError() from None
        if res.timed_out:
            attempt["status"] = LlmTimeout.code
            raise LlmTimeout()
        try:
            envelope = json.loads(res.stdout) if not res.overflow else None
        except (ValueError, RecursionError):
            envelope = None
        if not isinstance(envelope, dict):
            attempt["status"] = LlmError.code
            if res.overflow or res.returncode == 0:
                raise LlmError()
            raise _classify(res.stderr.decode("utf-8", "replace"), None)
        usage = _usage(envelope.get("usage"))
        if usage is not None:
            record["usage"].append(usage)
            attempt["usage"] = usage
        result_field = envelope.get("result")
        if envelope.get("is_error") is True or res.returncode != 0:
            attempt["status"] = LlmError.code
            body = result_field if isinstance(result_field, str) else ""
            status = envelope.get("api_error_status")
            raise _classify(body + "\n" + res.stderr.decode("utf-8", "replace"), status)
        attempt["status"] = "end_turn"
        if schema is None:
            if not isinstance(result_field, str) or not result_field:
                raise MissingOutput()
            return result_field
        structured = envelope.get("structured_output")
        if structured is None:
            if not isinstance(result_field, str) or not result_field:
                raise MissingOutput()
            try:
                structured = json.loads(result_field)
            except (ValueError, RecursionError):
                raise InvalidJson() from None
        try:
            return schema.model_validate_json(json.dumps(structured))
        except ValidationError as error:
            logging.getLogger(__name__).warning(
                "LLM response validation failed: ValidationError reason=%s",
                failure_reason(error),
            )
            raise SchemaMismatch() from None


def _usage(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    try:
        created = int(raw.get("cache_creation_input_tokens") or 0)
        usage = TokenUsage(
            int(raw.get("input_tokens") or 0),
            int(raw.get("output_tokens") or 0),
            int(raw.get("cache_read_input_tokens") or 0),
            0,
            created,
        )
    except (TypeError, ValueError):
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_creation_input_tokens": created,
    }
