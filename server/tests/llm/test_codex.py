"""Codex adapter contract: fake runner only, never invokes the installed CLI."""

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from weaver.config import LlmConfig
from weaver.llm import ClaudeCliClient, CodexCliClient, create_client
from weaver.llm.cli import RunResult
from weaver.llm.codex import codex_status
from weaver.llm.errors import (
    AuthenticationError,
    InvalidJson,
    LlmError,
    LlmTimeout,
    MissingOutput,
    RateLimited,
    SchemaMismatch,
    UsageLimited,
)
from weaver.llm.ledger import Ledger


class Answer(BaseModel):
    count: int
    title: str = "default"


class FakeRunner:
    def __init__(self, result=None, output='{"count":3,"title":null}'):
        self.result = result or RunResult(
            0,
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 3400,
                        "output_tokens": 800,
                        "cached_input_tokens": 100,
                    },
                }
            ).encode(),
            b"",
        )
        self.output = output
        self.calls = []
        self.schema = None

    def __call__(self, argv, *, stdin, env, cwd, timeout):
        self.calls.append((list(argv), stdin, dict(env), cwd, timeout))
        assert list(cwd.iterdir()) == []
        if "--output-schema" in argv:
            self.schema = json.loads(
                Path(argv[argv.index("--output-schema") + 1]).read_text()
            )
        if self.output is not None:
            Path(argv[argv.index("-o") + 1]).write_text(self.output)
        return self.result


@pytest.fixture
def setup(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("unused")
    binary.chmod(0o700)
    config = LlmConfig("codex_cli", codex_path=binary, max_cli_calls_per_day=2)
    ledger = Ledger(tmp_path / "llm.sqlite3")
    return config, ledger, tmp_path


def client_for(setup, runner, **changes):
    config, ledger, state = setup
    return CodexCliClient(
        replace(config, **changes),
        ledger,
        state_root=state,
        runner=runner,
        environ={
            "HOME": "/fake",
            "PATH": "/bin",
            "OPENAI_API_KEY": "SECRET",
            "CODEX_HOME": "/untrusted",
            "CODEX_CONFIG": "untrusted",
        },
    )


def test_argv_stdin_schema_usage_cleanup_and_log(setup, caplog):
    runner = FakeRunner()
    client = client_for(setup, runner, codex_model="test-model", codex_effort="high")
    with caplog.at_level(logging.INFO):
        result = client.generate_json(
            "wiki.digest", "SYSTEM-SECRET", "INPUT-SECRET", Answer
        )
    assert result == Answer(count=3)
    argv, stdin, env, cwd, timeout = runner.calls[0]
    assert json.loads(stdin) == {
        "instructions": "SYSTEM-SECRET",
        "input": "INPUT-SECRET",
    }
    assert "SECRET" not in str(argv)
    assert env == {"HOME": "/fake", "PATH": "/bin"}
    for flag in (
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
    ):
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert 'approval_policy="never"' in argv
    assert 'web_search="disabled"' in argv
    for feature in ("shell_tool", "plugins", "apps", "hooks", "multi_agent"):
        assert argv[argv.index(feature) - 1] == "--disable"
    assert argv[argv.index("-m") + 1] == "test-model"
    assert 'model_reasoning_effort="high"' in argv
    assert not cwd.exists() and 0 < timeout <= 120
    assert runner.schema["required"] == ["count", "title"]
    assert runner.schema["additionalProperties"] is False
    record = setup[1].records()[0]
    assert record["backend"] == "codex_cli" and record["status"] == "succeeded"
    assert record["usage"][0]["cache_read_input_tokens"] == 100
    assert record["usage"][0]["input_tokens"] == 3400
    assert record["budget_held_usd"] == 0
    lines = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 1
    assert "wiki.digest codex_cli succeeded" in lines[0]
    assert "in=3.4K out=800 (today 1/2)" in lines[0]
    assert "SECRET" not in caplog.text + str(record)


@pytest.mark.parametrize(
    "result,output,error",
    [
        (RunResult(1, b"", b"401 authentication failed"), None, AuthenticationError),
        (
            RunResult(1, b"not json", b"authentication failed"),
            None,
            AuthenticationError,
        ),
        (
            RunResult(1, b'{"type":"error","message":"quota exceeded"}', b""),
            None,
            UsageLimited,
        ),
        (RunResult(1, b"", b"too many requests"), None, RateLimited),
        (RunResult(1, b"", b"", timed_out=True), None, LlmTimeout),
        (RunResult(0, b"", b"", overflow=True), None, LlmError),
        (RunResult(0, b"invalid", b""), None, LlmError),
        (None, None, MissingOutput),
        (None, "invalid", InvalidJson),
        (None, '{"count":"bad"}', SchemaMismatch),
    ],
)
def test_errors_are_redacted_recorded_and_logged(setup, caplog, result, output, error):
    runner = FakeRunner(result, output)
    client = client_for(setup, runner)
    with caplog.at_level(logging.INFO), pytest.raises(error):
        client.generate_json("test.call", "secret", "secret", Answer)
    record = setup[1].records()[0]
    assert record["status"] == error.code
    assert record["attempt_records"][0]["status"] == error.code
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
    assert list(client.workdir.iterdir()) == []


def test_shared_cap_with_claude_and_factory(setup):
    config, ledger, state = setup
    runner = FakeRunner(output="hello")
    client = client_for(setup, runner, max_cli_calls_per_day=1)
    assert client.generate_text("test.call", "s", "u") == "hello"
    argv = runner.calls[0][0]
    assert "-m" not in argv and "--output-schema" not in argv
    claude = ClaudeCliClient(
        replace(
            config,
            backend="claude_cli",
            claude_path=config.codex_path,
            max_cli_calls_per_day=1,
        ),
        ledger,
        state_root=state,
        runner=lambda *a, **kw: pytest.fail("cap bypass"),
        environ={},
    )
    with pytest.raises(UsageLimited):
        claude.generate_text("test.call", "s", "u")
    with pytest.raises(UsageLimited):
        client.generate_text("test.call", "s", "u")
    assert len(runner.calls) == 1
    assert isinstance(create_client(config, state_root=state), CodexCliClient)


@pytest.mark.parametrize(
    "code,timed_out,expected",
    [
        (0, False, "configured"),
        (1, False, "codex_not_logged_in"),
        (0, True, "codex_status_timeout"),
    ],
)
def test_login_probe(setup, code, timed_out, expected):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"] == {"HOME": "/fake"}
        return RunResult(code, b"", b"", timed_out=timed_out)

    status = codex_status(
        setup[0], {"HOME": "/fake", "OPENAI_API_KEY": "secret"}, runner=runner, ttl=0
    )
    assert status.reason == expected
    assert calls[0][1:] == ["login", "status"]


def test_missing_binary_and_gate(setup, monkeypatch):
    from weaver.llm.cli import CliStatus
    from weaver.runtime.pipeline import llm_status

    monkeypatch.setattr(
        "weaver.runtime.pipeline.codex_status", lambda *a: CliStatus(True, "configured")
    )
    assert llm_status(setup[0], {}).available
    assert (
        codex_status(
            replace(setup[0], codex_path=Path("/nonexistent/codex")), {}
        ).reason
        == "codex_not_found"
    )


def test_tool_event_is_rejected(setup):
    runner = FakeRunner(
        RunResult(
            0, b'{"type":"item.completed","item":{"type":"command_execution"}}', b""
        )
    )
    with pytest.raises(LlmError):
        client_for(setup, runner).generate_text("test.call", "s", "u")
    assert setup[1].records()[0]["status"] == LlmError.code


def test_cached_status_uses_home_and_ttl(setup):
    calls = []

    def runner(argv, **kwargs):
        calls.append(kwargs["env"])
        return RunResult(0, b"", b"")

    config = setup[0]
    assert codex_status(config, {"HOME": "/cache-a"}, runner=runner, now=0).available
    assert codex_status(config, {"HOME": "/cache-a"}, runner=runner, now=1).available
    assert len(calls) == 1
    assert codex_status(config, {"HOME": "/cache-b"}, runner=runner, now=1).available
    assert len(calls) == 2
    codex_status(config, {"HOME": "/cache-a"}, runner=runner, now=61)
    assert len(calls) == 3
