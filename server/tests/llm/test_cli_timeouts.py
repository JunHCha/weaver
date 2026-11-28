"""Deadline and daily safety policy with synthetic CLIs and clocks only."""

import json
import sys
from dataclasses import replace
from datetime import datetime as RealDatetime

import pytest

from weaver.config import ConfigError, LlmConfig
from weaver.llm import AnthropicClient, ClaudeCliClient, CodexCliClient, create_client
from weaver.llm.cli import RunResult
from weaver.llm.errors import LlmTimeout, WikiTimeoutBudgetExceeded
from weaver.llm.ledger import Ledger, MemoryLedger

pytestmark = pytest.mark.usefixtures("transport_policy")


@pytest.mark.parametrize("backend", ["claude_cli", "codex_cli"])
@pytest.mark.parametrize("behavior", ["progress", "stalled", "stderr", "overall"])
def test_progress_watchdog_with_real_synthetic_cli(tmp_path, backend, behavior, caplog):
    import time

    binary = tmp_path / "fake-progress-cli"
    binary.write_text(
        "#!/bin/sh\n"
        # A shell fixture avoids Python interpreter startup dominating the
        # short inactivity window on a loaded macOS machine.
        "/bin/cat >/dev/null\n"
        "output=''\nprevious=''\n"
        'for arg do\n  if [ "$previous" = "-o" ]; then output="$arg"; fi\n'
        '  previous="$arg"\ndone\n'
        f"behavior={behavior}\n"
        'i=0\nwhile [ "$i" -lt 24 ]; do\n'
        '  if [ "$behavior" = progress ] || [ "$behavior" = overall ] || '
        '{ [ "$behavior" = stalled ] && [ "$i" -eq 0 ]; }; then\n'
        '    printf \'%s\\n\' \'{"type":"item.updated","text":"private-progress"}\'\n'
        '  elif [ "$behavior" = stderr ]; then\n'
        "    printf '%s\\n' private-stderr >&2\n"
        "  fi\n  /bin/sleep 0.2\n  i=$((i+1))\ndone\n"
        'if [ -n "$output" ]; then\n'
        '  printf ok >"$output"\n'
        "  printf '%s\\n' '{\"type\":\"turn.completed\"}'\n"
        "else\n"
        '  printf \'%s\\n\' \'{"type":"result","result":"ok","usage":{"output_tokens":7}}\'\n'
        "fi\n"
    )
    binary.chmod(0o700)
    client = create_client(
        LlmConfig(
            backend,
            claude_path=binary,
            codex_path=binary,
            cli_idle_timeout_seconds=1.5,
            cli_max_runtime_seconds=3 if behavior == "overall" else 10,
        ),
        state_root=tmp_path,
    )
    started = time.monotonic()
    if behavior == "progress":
        assert client.generate_text("loom.summary", "s", "u") == "ok"
        assert time.monotonic() - started > 4
    else:
        with pytest.raises(LlmTimeout):
            client.generate_text("loom.summary", "s", "u")
        assert time.monotonic() - started < 4.8
    [record] = Ledger(tmp_path / "llm.sqlite3").records()
    assert record["status"] == ("succeeded" if behavior == "progress" else "timeout")
    if behavior != "progress":
        diagnostic = record["timeout_diagnostics"]
        assert diagnostic["reason"] == ("overall" if behavior == "overall" else "idle")
        if behavior != "overall":
            assert diagnostic["idle_seconds"] >= 1.5
    elif backend == "claude_cli":
        assert record["usage"][0]["output_tokens"] == 7
    assert "private" not in caplog.text + json.dumps(record)
    with client.ledger._connect() as db:
        assert db.execute("SELECT calls FROM cli_daily").fetchone()[0] == 1
        rows = db.execute("SELECT calls FROM cli_timeouts").fetchall()
        assert rows == ([] if behavior == "progress" else [(1,)])
    if backend == "codex_cli":
        assert list((tmp_path / "codex-cli-workdir").iterdir()) == []


@pytest.mark.parametrize("backend", ["claude_cli", "codex_cli"])
def test_cli_client_logs_legacy_key_deprecation_once(
    tmp_path, caplog, monkeypatch, backend
):
    import weaver.config as config_module

    monkeypatch.setattr(config_module, "_warned_cli_timeout_aliases", False)
    config = LlmConfig(backend, cli_timeout_seconds=300)
    assert "Deprecated" not in caplog.text
    for _ in range(2):
        create_client(config, state_root=tmp_path)
    assert caplog.text.count("Deprecated llm.cli_timeout_seconds") == 1


@pytest.mark.parametrize(
    "value", [True, 0, -1, 86401, float("inf"), float("nan"), "secret"]
)
def test_invalid_overall_runtime_is_redacted(value):
    with pytest.raises(ConfigError, match="cli_max_runtime_seconds") as error:
        LlmConfig(cli_max_runtime_seconds=value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("backend", ["claude_cli", "codex_cli"])
def test_delayed_fake_cli_uses_configured_timeout_and_wiki_override(tmp_path, backend):
    binary = tmp_path / "fake-cli"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "time.sleep(0.25)\n"
        "if '-o' in sys.argv:\n"
        "    open(sys.argv[sys.argv.index('-o')+1], 'w').write('ok')\n"
        "    print(json.dumps({'type': 'turn.completed'}))\n"
        "else:\n"
        "    print(json.dumps({'result': 'ok'}))\n"
    )
    binary.chmod(0o700)
    config = LlmConfig(
        backend,
        claude_path=binary,
        codex_path=binary,
        cli_idle_timeout_seconds=0.08,
        wiki_cli_idle_timeout_seconds=2,
    )
    client = create_client(config, state_root=tmp_path)
    with pytest.raises(LlmTimeout):
        client.generate_text("loom.summary", "private-system", "private-body")
    assert client.generate_text("wiki.compile_batch", "s", "u") == "ok"
    # A Wiki call must not mutate the common timeout for subsequent work.
    with pytest.raises(LlmTimeout):
        client.generate_text("spinner.revise", "s", "u")


@pytest.mark.parametrize("backend", ["claude_cli", "codex_cli"])
def test_default_and_custom_deadlines_reach_cli_runner(tmp_path, backend):
    binary = tmp_path / "fake"
    binary.touch(mode=0o700)
    captured = []

    def runner(argv, *, stdin, env, cwd, timeout):
        captured.append(timeout)
        return RunResult(-9, b"", b"", timed_out=True)

    config = LlmConfig(backend, claude_path=binary, codex_path=binary)
    cls = ClaudeCliClient if backend == "claude_cli" else CodexCliClient
    ledger = Ledger(tmp_path / "llm.sqlite3")
    for settings, expected in (
        (config, [600, 600]),
        (
            replace(
                config, cli_idle_timeout_seconds=450, wiki_cli_idle_timeout_seconds=900
            ),
            [450, 900],
        ),
    ):
        client = cls(settings, ledger, state_root=tmp_path, runner=runner)
        for task, deadline in zip(
            ("loom.summary", "wiki.compile_batch"), expected, strict=True
        ):
            with pytest.raises(LlmTimeout):
                client.generate_text(task, "s", "u")
            assert deadline - 1 < captured[-1] <= deadline


@pytest.mark.parametrize(
    "field", ["cli_idle_timeout_seconds", "wiki_cli_idle_timeout_seconds"]
)
@pytest.mark.parametrize(
    "value", [True, 0, -1, 901, float("inf"), float("nan"), "secret"]
)
def test_invalid_cli_timeout_settings_are_redacted(field, value):
    with pytest.raises(ConfigError, match=field) as error:
        LlmConfig(**{field: value})
    assert "secret" not in str(error.value)


def test_api_deadline_is_unchanged(tmp_path):
    config = LlmConfig(cli_idle_timeout_seconds=900, wiki_cli_idle_timeout_seconds=900)
    client = create_client(config, state_root=tmp_path)
    assert isinstance(client, AnthropicClient)
    assert client.deadline_seconds == 120
    with pytest.raises(ValueError):
        AnthropicClient(config, client.ledger, deadline_seconds=121)


@pytest.mark.parametrize("cap", [4, 5])
def test_daily_timeout_guard_persists_is_shared_and_resets_next_day(
    tmp_path, monkeypatch, caplog, cap
):
    import weaver.llm.cli as cli_module
    import weaver.llm.codex as codex_module

    class Clock(RealDatetime):
        current = RealDatetime(2026, 1, 1, 12).astimezone()

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(cli_module, "datetime", Clock)
    monkeypatch.setattr(codex_module, "datetime", Clock)
    binary = tmp_path / "fake"
    binary.touch(mode=0o700)
    calls = []

    def runner(argv, *, stdin, env, cwd, timeout):
        calls.append(1)
        return RunResult(-9, b"", b"", timed_out=True)

    config = LlmConfig(
        "claude_cli", claude_path=binary, codex_path=binary, max_cli_calls_per_day=cap
    )
    ledger = Ledger(tmp_path / "llm.sqlite3")
    client = ClaudeCliClient(config, ledger, state_root=tmp_path, runner=runner)
    # Count all CLI timeouts, including source work, against the Wiki safeguard.
    for _ in range((cap + 1) // 2):
        with pytest.raises(LlmTimeout):
            client.generate_text("loom.summary", "private", "private")
    count = len(calls)
    other = CodexCliClient(
        replace(config, backend="codex_cli"),
        Ledger(ledger.path),
        state_root=tmp_path,
        runner=runner,
    )
    with pytest.raises(WikiTimeoutBudgetExceeded):
        other.generate_text("wiki.compile_batch", "private", "private")
    assert len(calls) == count
    # Refusing Wiki does not consume another call or stop source processing.
    with pytest.raises(LlmTimeout):
        client.generate_text("loom.summary", "s", "u")
    assert len(calls) == count + 1
    assert "daily timeout allowance reached" in caplog.text
    assert "private" not in caplog.text
    assert "private" not in json.dumps(ledger.records())
    Clock.current = RealDatetime(2026, 1, 2, 12).astimezone()
    with pytest.raises(LlmTimeout):
        other.generate_text("wiki.compile_batch", "s", "u")
    assert len(calls) == count + 2
    # A dry-run ledger sees the persisted safeguard without changing disk state.
    before = ledger.records()
    memory = MemoryLedger(ledger.path)
    with pytest.raises(WikiTimeoutBudgetExceeded):
        memory.take_daily_call("2026-01-01", cap, wiki=True)
    assert ledger.records() == before


@pytest.mark.parametrize("timeout", [False, True])
def test_codex_large_input_with_output_schema_and_timeout_diagnostics(
    tmp_path, caplog, timeout
):
    from pydantic import BaseModel

    class Answer(BaseModel):
        count: int

    binary = tmp_path / "fake-large-codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "payload = json.load(sys.stdin)\n"
        "assert len(payload['input'].split()) >= 40000\n"
        "schema = json.load(open(sys.argv[sys.argv.index('--output-schema') + 1]))\n"
        "assert schema['properties']['count']['type'] == 'integer'\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'private-id'}), flush=True)\n"
        "print(json.dumps({'type': 'item.started', 'item': {'text': 'private-reasoning'}}), flush=True)\n"
        + ("time.sleep(60)\n" if timeout else "")
        + "open(sys.argv[sys.argv.index('-o')+1], 'w').write('{\"count\":42}')\n"
        "print(json.dumps({'type': 'turn.completed'}), flush=True)\n"
    )
    binary.chmod(0o700)
    client = create_client(
        LlmConfig(
            "codex_cli",
            codex_path=binary,
            wiki_cli_idle_timeout_seconds=3 if timeout else 30,
        ),
        state_root=tmp_path,
    )
    large = "private-input " * 40000
    if timeout:
        with pytest.raises(LlmTimeout):
            client.generate_json("wiki.compile_batch", "private-system", large, Answer)
        record = Ledger(tmp_path / "llm.sqlite3").records()[0]
        diagnostic = record["timeout_diagnostics"]
        assert diagnostic["last_event"] == "item.started"
        assert diagnostic["input_bytes"] > 500000
        assert diagnostic["schema_bytes"] > 0
        assert diagnostic["elapsed_seconds"] >= 3
        assert diagnostic["stdout_bytes"] > 0
        assert diagnostic["output_bytes"] == 0
        assert "last_event=item.started" in caplog.text
        assert "stdout_bytes=" in caplog.text and "schema_bytes=" in caplog.text
        for secret in (
            "private-input",
            "private-system",
            "private-id",
            "private-reasoning",
        ):
            assert secret not in caplog.text + json.dumps(record)
    else:
        assert (
            client.generate_json("wiki.compile_batch", "s", large, Answer).count == 42
        )


@pytest.mark.parametrize(
    "events, expected",
    [
        (b"", "none"),
        (b'{"type":"private-event-name"}\n', "unknown"),
        (b'{"type":"turn.started"}\n{"type":', "turn.started"),
        (b'{"type":["private"]}\n', "unknown"),
    ],
)
def test_codex_timeout_diagnostics_redact_untrusted_and_partial_events(
    tmp_path, caplog, events, expected
):
    import time

    from weaver.llm.codex import _timeout_diagnostics

    output = tmp_path / "output"
    output.write_bytes(b"private-output")
    diagnostic = _timeout_diagnostics(
        events, output, started=time.monotonic(), input_bytes=20, schema_bytes=30
    )
    assert diagnostic["last_event"] == expected
    assert diagnostic["output_bytes"] == 14
    assert "private" not in caplog.text + json.dumps(diagnostic)


def test_existing_timeout_ledger_migrates_and_finish_is_idempotent(tmp_path):
    ledger = Ledger(tmp_path / "llm.sqlite3")
    now = RealDatetime.now().astimezone()
    day = now.date().isoformat()
    record = {
        "status": "pending",
        "backend": "codex_cli",
        "attempts": 1,
        "cli_day": day,
        "started_at": now.isoformat(),
    }
    call = ledger.reserve(record, 0)
    record["status"] = "timeout"
    ledger.finish(call, record, 0)
    ledger.finish(call, record, 0)
    # At cap=3, one timeout is below half. Two would block Wiki.
    assert ledger.take_daily_call(day, 3, wiki=True)
    with ledger._connect() as db:
        db.execute("DROP TABLE cli_timeouts")
    reopened = Ledger(ledger.path)
    with pytest.raises(WikiTimeoutBudgetExceeded):
        reopened.take_daily_call(day, 2, wiki=True)
    assert reopened.take_daily_call(day, 3, wiki=True)


@pytest.mark.parametrize("reason", ["timeout", "stop"])
def test_codex_real_subprocess_timeout_or_signal_removes_process_group_and_temp_dir(
    tmp_path, reason
):
    import os
    import signal
    import threading
    import time

    from weaver.llm.cli import cancel_cli_on_stop
    from weaver.llm.errors import LlmInterrupted
    from weaver.runtime.loop import stop_on_signals

    pidfile = tmp_path / "pids.json"
    binary = tmp_path / "fake-tree"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
        "print(json.dumps({'type': 'turn.started'}), flush=True)\n"
        "time.sleep(60)\n"
    )
    binary.chmod(0o700)
    client = create_client(
        LlmConfig(
            "codex_cli",
            codex_path=binary,
            cli_idle_timeout_seconds=3 if reason == "timeout" else 900,
        ),
        state_root=tmp_path,
    )
    stop = threading.Event()

    def send_stop():
        for _ in range(300):
            if pidfile.exists() and pidfile.stat().st_size:
                break
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_stop) if reason == "stop" else None
    with stop_on_signals(stop), cancel_cli_on_stop(stop):
        if sender is not None:
            sender.start()
        with pytest.raises(LlmTimeout if reason == "timeout" else LlmInterrupted):
            client.generate_text("loom.summary", "s", "u")
        if sender is not None:
            sender.join(timeout=5)
            assert not sender.is_alive()
    pids = json.loads(pidfile.read_text())
    for pid in pids:
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"fake process survived: {pid}")
    assert list((tmp_path / "codex-cli-workdir").iterdir()) == []
    [record] = Ledger(tmp_path / "llm.sqlite3").records()
    assert record["status"] == ("timeout" if reason == "timeout" else "interrupted")
    assert stop.is_set() == (reason == "stop")


def test_already_stopped_runner_does_not_spawn(tmp_path):
    import threading

    from weaver.llm.cli import subprocess_runner

    stop = threading.Event()
    stop.set()
    result = subprocess_runner(
        ["/nonexistent/must-not-spawn"],
        stdin=b"",
        env={},
        cwd=tmp_path,
        timeout=900,
        stop_event=stop,
    )
    assert result.stopped and not result.timed_out
