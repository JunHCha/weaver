"""Official Codex exec adapter, with no ambient user configuration or tools.

Only the CLI owns authentication. No auth files or OAuth tokens are read here.
The supported option/feature baseline is codex-cli 0.167.0.
"""

import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from weaver.config import LlmConfig

from .cli import (
    MAX_OUTPUT_BYTES,
    STATUS_TTL_SECONDS,
    ClaudeCliClient,
    CliStatus,
    Runner,
    _classify,
    _usage,
    child_env,
    subprocess_runner,
)
from .errors import (
    AuthenticationError,
    InvalidJson,
    LlmError,
    LlmTimeout,
    MissingOutput,
    SchemaMismatch,
    UsageLimited,
)
from .schemas import openai_schema, restore_omitted_defaults

# Disabling shell is necessary: read-only + never alone still allows reads.
_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apply_patch_freeform",
    "apps",
    "plugins",
    "hooks",
    "codex_hooks",
    "plugin_hooks",
    "multi_agent",
    "collab",
    "js_repl",
    "code_mode",
    "computer_use",
    "browser_use",
    "image_generation",
    "view_image",
    "memory_tool",
    "memories",
    "tool_suggest",
    "skill_search",
    "request_permissions_tool",
    "default_mode_request_user_input",
    "sleep_tool",
)
_status_cache: dict[tuple[str, str | None], tuple[float, CliStatus]] = {}
_status_lock = threading.Lock()


def find_codex(config: LlmConfig, environ: Mapping[str, str]) -> str | None:
    if config.codex_path is not None:
        path = str(config.codex_path)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    return shutil.which("codex", path=environ.get("PATH", os.defpath))


def codex_status(
    config: LlmConfig,
    environ: Mapping[str, str],
    *,
    runner: Runner = subprocess_runner,
    ttl: float = STATUS_TTL_SECONDS,
    now: float | None = None,
) -> CliStatus:
    binary = find_codex(config, environ)
    if binary is None:
        return CliStatus(False, "codex_not_found")
    clock = time.monotonic() if now is None else now
    key = (binary, environ.get("HOME"))
    with _status_lock:
        hit = _status_cache.get(key)
        if hit is not None and clock - hit[0] < ttl:
            return hit[1]
    try:
        res = runner(
            [binary, "login", "status"],
            stdin=b"",
            env=child_env(environ),
            cwd=Path.cwd(),
            timeout=15,
        )
        status = CliStatus(
            res.returncode == 0 and not res.timed_out and not res.overflow,
            "codex_status_timeout"
            if res.timed_out
            else (
                "configured"
                if res.returncode == 0 and not res.overflow
                else "codex_not_logged_in"
            ),
        )
    except OSError:
        status = CliStatus(False, "codex_unavailable")
    with _status_lock:
        _status_cache[key] = (clock, status)
    return status


class CodexCliClient(ClaudeCliClient):
    """Share the CLI lifecycle, ledger, daily cap and subprocess containment."""

    backend = "codex_cli"
    workdir_name = "codex-cli-workdir"

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.config.codex_model or "codex-default"

    def _run[T: BaseModel](
        self,
        record: dict[str, Any],
        started: float,
        system: str,
        user: str,
        schema: type[T] | None,
    ) -> Any:
        binary = find_codex(self.config, self.environ)
        if binary is None:
            raise AuthenticationError()
        wire_schema = openai_schema(schema) if schema is not None else None
        self.workdir.mkdir(mode=0o700, exist_ok=True)
        # Unique private empty cwd, isolated from prior calls and project config.
        with tempfile.TemporaryDirectory(dir=self.workdir) as directory:
            root = Path(directory)
            cwd = root / "work"
            cwd.mkdir(mode=0o700)
            output = root / "output.json"
            argv = [
                binary,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "-s",
                "read-only",
                "-C",
                str(cwd),
                "-c",
                'approval_policy="never"',
                "-c",
                'web_search="disabled"',
                "-c",
                "mcp_servers={}",
                "-c",
                "project_doc_max_bytes=0",
                "-c",
                "tools.update_plan.enabled=false",
                "-c",
                'developer_instructions="Complete the supplied inference task using only '
                "its input. Do not call tools, execute commands, or access files. "
                'Return only the requested final output."',
            ]
            for feature in _DISABLED_FEATURES:
                argv += ["--disable", feature]
            if self.config.codex_model is not None:
                argv += ["-m", self.config.codex_model]
            if self.config.codex_effort is not None:
                argv += [
                    "-c",
                    "model_reasoning_effort=" + json.dumps(self.config.codex_effort),
                ]
            if wire_schema is not None:
                schema_file = root / "schema.json"
                schema_file.write_text(json.dumps(wire_schema), encoding="utf-8")
                schema_file.chmod(0o600)
                argv += ["--output-schema", str(schema_file)]
            argv += ["-o", str(output), "--json", "-"]
            remaining = self.deadline_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise LlmTimeout()
            day = datetime.now().astimezone().date().isoformat()
            if not self.ledger.take_daily_call(day, self.config.max_cli_calls_per_day):
                raise UsageLimited()
            record["attempts"] = 1
            attempt: dict[str, Any] = {"status": "pending", "usage": None}
            record["attempt_records"].append(attempt)
            # stdin preserves privacy. Explicitly serialize role boundaries;
            # Codex exec has no separate system-prompt argument.
            prompt = json.dumps(
                {"instructions": system, "input": user}, ensure_ascii=False
            )
            try:
                res = self.runner(
                    argv,
                    stdin=prompt.encode(),
                    env=child_env(self.environ),
                    cwd=cwd,
                    timeout=remaining,
                )
                if res.timed_out:
                    raise LlmTimeout()
                if res.overflow:
                    raise LlmError()
                completed = False
                failure = ""
                for line in res.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        if res.returncode != 0:
                            raise _classify(
                                res.stderr.decode("utf-8", "replace")
                                + res.stdout.decode("utf-8", "replace"),
                                None,
                            ) from None
                        raise LlmError() from None
                    if not isinstance(event, dict):
                        raise LlmError()
                    if event.get("type") == "turn.completed":
                        completed = True
                        usage = _usage(event.get("usage"))
                        if usage is not None:
                            # Codex input_tokens includes cached input tokens.
                            raw = event.get("usage", {})
                            cached = raw.get("cached_input_tokens", 0)
                            if type(cached) is int and cached >= 0:
                                usage["cache_read_input_tokens"] = cached
                            record["usage"].append(usage)
                            attempt["usage"] = usage
                    if event.get("type") in {"turn.failed", "error"}:
                        failure += json.dumps(event)
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") in {
                        "command_execution",
                        "mcp_tool_call",
                        "web_search",
                        "file_change",
                    }:
                        raise LlmError()  # Fail closed if tool policy regresses.
                if res.returncode != 0 or failure:
                    raise _classify(
                        failure + res.stderr.decode("utf-8", "replace"), None
                    )
                if not completed:
                    raise MissingOutput()
                if not output.exists():
                    raise MissingOutput()
                with output.open("rb") as stream:
                    body = stream.read(MAX_OUTPUT_BYTES + 1)
                if len(body) > MAX_OUTPUT_BYTES:
                    raise LlmError()
                if not body:
                    raise MissingOutput()
                if schema is None:
                    result: Any = body.decode("utf-8")
                else:
                    try:
                        structured = json.loads(body)
                    except ValueError:
                        raise InvalidJson() from None
                    try:
                        result = schema.model_validate_json(
                            json.dumps(restore_omitted_defaults(structured, schema))
                        )
                    except ValidationError:
                        raise SchemaMismatch() from None
                attempt["status"] = "end_turn"
                return result
            except LlmError as exc:
                attempt["status"] = exc.code
                raise
            except (OSError, ValueError, RecursionError):
                attempt["status"] = LlmError.code
                raise LlmError() from None
