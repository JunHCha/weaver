"""claude_cli adapter with fake runners/executables only; no real claude."""

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from weaver.config import LlmConfig
from weaver.llm import (
    AuthenticationError,
    ClaudeCliClient,
    InvalidJson,
    LlmError,
    LlmTimeout,
    MissingOutput,
    RateLimited,
    SchemaMismatch,
    UsageLimited,
    create_client,
)
from weaver.llm.cli import RunResult, child_env, cli_status, subprocess_runner
from weaver.llm.ledger import Ledger
from weaver.runtime.pipeline import llm_status


class Answer(BaseModel):
    count: int


SECRET_PROMPT = "통화 내용 SECRET-CALL-TEXT"
CLAUDE = Path("/opt/fake/claude")


def envelope(**fields: object) -> bytes:
    base = {"type": "result", "is_error": False, "result": "ok"}
    return json.dumps({**base, **fields}).encode()


class FakeRunner:
    def __init__(self, *results: RunResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, *, stdin, env, cwd, timeout):  # type: ignore[no-untyped-def]
        self.calls.append(
            {"argv": list(argv), "stdin": stdin, "env": dict(env), "cwd": cwd}
        )
        return self.results.pop(0)


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def make(
    tmp_path: Path, fake_claude: Path, *results: RunResult, cap: int = 30, env=None
):  # type: ignore[no-untyped-def]
    runner = FakeRunner(*results)
    ledger = Ledger(tmp_path / "llm.sqlite3")
    config = LlmConfig("claude_cli", claude_path=fake_claude, max_cli_calls_per_day=cap)
    client = ClaudeCliClient(
        config,
        ledger,
        state_root=tmp_path,
        runner=runner,
        environ=env
        if env is not None
        else {
            "HOME": "/h",
            "PATH": "/bin",
            "USER": "u",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": "/t",
            "ANTHROPIC_API_KEY": "sk-secret",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
            "CLAUDE_CODE_SIMPLE": "1",
            "OTHER": "x",
        },
    )
    return client, runner, ledger


def ok(**fields: object) -> RunResult:
    return RunResult(0, envelope(**fields), b"")


def test_text_success_isolation_and_ledger(tmp_path: Path, fake_claude: Path) -> None:
    usage = {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 1}
    client, runner, ledger = make(
        tmp_path, fake_claude, ok(result="hello", usage=usage)
    )
    assert client.generate_text("loom.test", "SYSTEM", SECRET_PROMPT) == "hello"
    call = runner.calls[0]
    argv = call["argv"]
    assert isinstance(argv, list)
    # prompt only on stdin, never argv
    assert call["stdin"] == SECRET_PROMPT.encode()
    assert not any("SECRET-CALL-TEXT" in a for a in argv)
    for flag in (
        "--print",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ):
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--bare" not in argv and "--json-schema" not in argv
    cwd = call["cwd"]
    assert isinstance(cwd, Path) and cwd.parent == tmp_path and not any(cwd.iterdir())
    [rec] = ledger.records()
    assert rec["backend"] == "claude_cli" and rec["status"] == "succeeded"
    assert rec["attempts"] == 1 and rec["cost_usd"] is None
    assert rec["cost_source"] == "unavailable" and rec["budget_held_usd"] == 0
    assert rec["usage"][0]["output_tokens"] == 7
    assert "hello" not in json.dumps(rec) and "SECRET" not in json.dumps(rec)


def test_child_env_is_allowlist_only(tmp_path: Path, fake_claude: Path) -> None:
    client, runner, _ = make(tmp_path, fake_claude, ok())
    client.generate_text("t.x", "s", "u")
    env = runner.calls[0]["env"]
    assert env == {
        "HOME": "/h",
        "PATH": "/bin",
        "USER": "u",
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": "/t",
    }
    leaked = child_env({"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_X": "1", "HOME": "/h"})
    assert leaked == {"HOME": "/h"}


def test_json_uses_transformed_schema_and_revalidates(
    tmp_path: Path, fake_claude: Path
) -> None:
    client, runner, _ = make(
        tmp_path,
        fake_claude,
        ok(structured_output={"count": 3}),
        ok(result='{"count": 4}'),
    )
    assert client.generate_json("t.x", "s", "u", Answer) == Answer(count=3)
    argv = runner.calls[0]["argv"]
    assert isinstance(argv, list)
    sent = json.loads(argv[argv.index("--json-schema") + 1])
    assert sent["properties"]["count"]["type"] == "integer"
    assert sent["additionalProperties"] is False
    assert client.generate_json("t.x", "s", "u", Answer).count == 4  # text fallback


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (ok(result="not json"), InvalidJson),
        (ok(structured_output={"count": "x"}), SchemaMismatch),
        (ok(result=""), MissingOutput),
        (RunResult(0, b"garbage", b""), LlmError),
    ],
)
def test_json_failures(tmp_path: Path, fake_claude: Path, result, error) -> None:  # type: ignore[no-untyped-def]
    client, _, ledger = make(tmp_path, fake_claude, result)
    with pytest.raises(error) as info:
        client.generate_json("t.x", "s", "u", Answer)
    assert type(info.value) is error
    assert ledger.records()[0]["status"] == error.code


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (
            ok(is_error=True, result="Not logged in · Please run /login"),
            AuthenticationError,
        ),
        (ok(is_error=True, result="x", api_error_status=401), AuthenticationError),
        (ok(is_error=True, result="Claude usage limit reached"), UsageLimited),
        (ok(is_error=True, result="x", api_error_status=429), RateLimited),
        (ok(is_error=True, result="boom"), LlmError),
        (RunResult(1, b"", b"Error: not logged in"), AuthenticationError),
        (RunResult(1, b"", b"weird"), LlmError),
        (RunResult(0, b"", b"", overflow=True), LlmError),
    ],
)
def test_error_mapping_without_leaking(
    tmp_path: Path, fake_claude: Path, result, error
) -> None:  # type: ignore[no-untyped-def]
    client, _, ledger = make(tmp_path, fake_claude, result)
    with pytest.raises(error) as info:
        client.generate_text("t.x", "s", "u")
    assert type(info.value) is error
    assert str(info.value) == error.code
    assert "logged" not in json.dumps(ledger.records())


def test_timeout(tmp_path: Path, fake_claude: Path) -> None:
    client, _, ledger = make(
        tmp_path, fake_claude, RunResult(-9, b"", b"", timed_out=True)
    )
    with pytest.raises(LlmTimeout):
        client.generate_text("t.x", "s", "u")
    assert ledger.records()[0]["status"] == "timeout"


def test_daily_cap_refuses_without_calling(
    tmp_path: Path, fake_claude: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, runner, ledger = make(tmp_path, fake_claude, ok(), ok(), cap=2)
    client.generate_text("t.x", "s", "u")
    client.generate_text("t.x", "s", "u")
    with caplog.at_level("WARNING"):
        with pytest.raises(UsageLimited):
            client.generate_text("t.x", "s", "u")
    assert len(runner.calls) == 2
    assert "daily call cap" in caplog.text
    assert ledger.records()[-1]["status"] == "usage_limited"


def test_timeouts_still_retryable_until_cap_then_usage_limited(
    tmp_path: Path, fake_claude: Path
) -> None:
    timeout = RunResult(-9, b"", b"", timed_out=True)
    client, runner, _ = make(tmp_path, fake_claude, timeout, timeout, cap=2)
    for _ in range(2):
        with pytest.raises(LlmTimeout):
            client.generate_text("t.x", "s", "u")
    with pytest.raises(UsageLimited):
        client.generate_text("t.x", "s", "u")
    assert len(runner.calls) == 2  # the capped attempt never spawns a process


def test_daily_cap_is_atomic_across_clients(tmp_path: Path) -> None:
    import threading

    ledger = Ledger(tmp_path / "llm.sqlite3")
    wins: list[bool] = []

    def take() -> None:
        wins.append(Ledger(tmp_path / "llm.sqlite3").take_daily_call("d", 5))

    threads = [threading.Thread(target=take) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 5 and not ledger.take_daily_call("d", 5)


def test_missing_binary_is_authentication_error(tmp_path: Path) -> None:
    runner = FakeRunner()
    config = LlmConfig("claude_cli", claude_path=tmp_path / "nope")
    client = ClaudeCliClient(
        config, Ledger(tmp_path / "l.sqlite3"), state_root=tmp_path, runner=runner
    )
    with pytest.raises(AuthenticationError):
        client.generate_text("t.x", "s", "u")
    assert not runner.calls


def test_create_client_dispatch(tmp_path: Path) -> None:
    client = create_client(LlmConfig("claude_cli"), state_root=tmp_path)
    assert isinstance(client, ClaudeCliClient)


# ---- real subprocess runner with a temporary fake executable ----------------


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake"
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_runner_passes_stdin_and_env(tmp_path: Path) -> None:
    fake = script(
        tmp_path,
        """
        import json, os, sys
        print(json.dumps({"in": sys.stdin.read(), "env": sorted(os.environ)}))
        """,
    )
    res = subprocess_runner(
        [str(fake)], stdin=b"hi", env={"PATH": "/bin"}, cwd=tmp_path, timeout=10
    )
    data = json.loads(res.stdout)
    assert data["in"] == "hi" and "ANTHROPIC_API_KEY" not in data["env"]


def test_runner_timeout_kills_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    fake = script(
        tmp_path,
        f"""
        import os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pidfile)!r}, "w").write(str(child.pid))
        time.sleep(60)
        """,
    )
    res = subprocess_runner([str(fake)], stdin=b"", env={}, cwd=tmp_path, timeout=1)
    assert res.timed_out
    pid = int(pidfile.read_text())
    import time

    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived the timeout")


def test_runner_output_cap(tmp_path: Path) -> None:
    fake = script(
        tmp_path,
        """
        import sys
        sys.stdout.write("x" * (5 * 1024 * 1024))
        """,
    )
    res = subprocess_runner([str(fake)], stdin=b"", env={}, cwd=tmp_path, timeout=20)
    assert res.overflow and len(res.stdout) <= 4 * 1024 * 1024


def test_runner_stderr_overflow_keeps_stdout(tmp_path: Path) -> None:
    fake = script(
        tmp_path,
        """
        import sys
        sys.stdout.write('{"result": "fine"}')
        sys.stdout.flush()
        sys.stderr.write("e" * 200_000)
        """,
    )
    res = subprocess_runner([str(fake)], stdin=b"", env={}, cwd=tmp_path, timeout=20)
    assert not res.overflow and not res.timed_out
    assert res.returncode == 0
    assert res.stdout == b'{"result": "fine"}'
    assert len(res.stderr) == 65536


# ---- availability -----------------------------------------------------------


def test_status_logged_in_and_cached(fake_claude: Path) -> None:
    runner = FakeRunner(
        RunResult(0, b'{"loggedIn": true}', b""),
        RunResult(0, b'{"loggedIn": false}', b""),
    )
    config = LlmConfig("claude_cli", claude_path=fake_claude)
    first = cli_status(config, {}, runner=runner, now=1000.0)
    assert first.available and first.reason == "configured"
    assert cli_status(config, {}, runner=runner, now=1010.0) == first
    assert len(runner.calls) == 1
    later = cli_status(config, {}, runner=runner, now=1100.0)
    assert not later.available and later.reason == "claude_not_logged_in"
    assert runner.calls[0]["argv"][1:] == ["auth", "status", "--json"]


def test_status_missing_binary(tmp_path: Path) -> None:
    config = LlmConfig("claude_cli", claude_path=tmp_path / "nope")
    status = llm_status(config, {"PATH": str(tmp_path)})
    assert (status.available, status.reason) == (False, "claude_not_found")
