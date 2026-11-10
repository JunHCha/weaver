"""LaunchAgent plist, fake launchctl, CLI wiring and an offline end-to-end tick."""

import hashlib
import json
import logging
import logging.handlers
import plistlib
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from weaver.app import cli
from weaver.config import load_config
from weaver.runtime import InstanceLock, launchd
from weaver.runtime.assemble import build_runtime

ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
CHANNELS = ("outbox", "receipts", "proposals", "decisions")
CAP = "cap_01ARZ3NDEKTSV4RRFFQ69G5FA0"


def write_config(tmp_path: Path, extra: str = "") -> Path:
    config = tmp_path / "config.toml"
    config.write_text("\n".join(f'{k} = "{k}"' for k in ROOTS) + "\n" + extra)
    for key in ROOTS:
        (tmp_path / key).mkdir(exist_ok=True)
    for channel in CHANNELS:
        (tmp_path / "sync_root" / channel).mkdir(exist_ok=True)
    return config


class FakeLaunchctl:
    """Models a job being loaded or not; print/bootout/bootstrap only."""

    def __init__(
        self,
        *,
        loaded: bool = False,
        stdout: str = "",
        bootout_rc: int = 0,
        bootstrap_failures: int = 0,
        bootstrap_rc: int = 5,
    ) -> None:
        self.calls: list[list[str]] = []
        self.loaded, self.stdout = loaded, stdout
        self.bootout_rc = bootout_rc
        self.bootstrap_failures, self.bootstrap_rc = bootstrap_failures, bootstrap_rc

    def __call__(self, args):
        self.calls.append(list(args))
        rc, out = 0, ""
        if args[0] == "print":
            rc, out = (0, self.stdout) if self.loaded else (113, "")
        elif args[0] == "bootout":
            rc = self.bootout_rc
            if rc == 0:
                self.loaded = False
        elif args[0] == "bootstrap":
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                rc = self.bootstrap_rc
            else:
                self.loaded = True
        return subprocess.CompletedProcess(args, rc, out, "")


@pytest.fixture
def fake_launchctl(monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "run_launchctl", fake)
    return fake


@pytest.fixture
def restore_logging():
    root = logging.getLogger("weaver")
    handlers, level, hook = list(root.handlers), root.level, sys.excepthook
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    sys.excepthook = hook


def render(tmp_path: Path) -> bytes:
    return launchd.render_plist(
        python=tmp_path / ".venv" / "bin" / "python",
        config_file=tmp_path / "config.toml",
        log_root=tmp_path / "logs",
        working_directory=tmp_path / "state",
    )


def test_plist_uses_absolute_paths_and_restart_limits(tmp_path):
    job = plistlib.loads(render(tmp_path))
    assert job["ProgramArguments"] == [
        str(tmp_path / ".venv" / "bin" / "python"),
        "-m",
        "weaver.app",
        "run",
        "--config",
        str(tmp_path / "config.toml"),
    ]
    assert job["KeepAlive"] == {"SuccessfulExit": False}
    assert job["ThrottleInterval"] == 30
    assert job["ExitTimeOut"] == 300  # above the 120 s LLM deadline
    assert job["EnvironmentVariables"]["WEAVER_SUPERVISED"] == "1"
    assert job["RunAtLoad"] is True
    assert job["StandardOutPath"] == str(tmp_path / "logs" / "launchd.out.log")
    assert job["StandardErrorPath"] == str(tmp_path / "logs" / "launchd.err.log")
    assert "PATH" not in job["EnvironmentVariables"]
    assert not any("KEY" in name for name in job["EnvironmentVariables"])
    with pytest.raises(ValueError):
        launchd.render_plist(
            python=Path("python"),
            config_file=tmp_path,
            log_root=tmp_path,
            working_directory=tmp_path,
        )


@pytest.mark.skipif(shutil.which("plutil") is None, reason="macOS plutil only")
def test_plist_passes_plutil_lint(tmp_path):
    path = tmp_path / "agent.plist"
    path.write_bytes(render(tmp_path))
    result = subprocess.run(
        ["plutil", "-lint", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout


def test_install_and_uninstall_with_fake_launchctl(tmp_path, monkeypatch):
    fake = FakeLaunchctl(loaded=True)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    job = "gui/501/io.github.junhcha.weaver"
    target = launchd.install(
        render(tmp_path), home=tmp_path, launchctl=fake, sleep=lambda s: None
    )
    assert target == tmp_path / "Library/LaunchAgents/io.github.junhcha.weaver.plist"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert fake.calls == [
        ["print", job],
        ["bootout", job],
        ["print", job],
        ["bootstrap", "gui/501", str(target)],
    ]
    assert launchd.uninstall(home=tmp_path, launchctl=fake) is True
    assert not target.exists() and not fake.loaded
    assert launchd.uninstall(home=tmp_path, launchctl=fake) is False


def test_uninstall_keeps_plist_when_bootout_fails(tmp_path):
    fake = FakeLaunchctl()
    target = launchd.install(render(tmp_path), home=tmp_path, launchctl=fake)
    fake.bootout_rc = 5
    with pytest.raises(launchd.LaunchdError, match="bootout exited 5"):
        launchd.uninstall(home=tmp_path, launchctl=fake)
    assert target.exists() and fake.loaded


def test_bootstrap_is_retried_once(tmp_path):
    sleeps = []
    fake = FakeLaunchctl(bootstrap_failures=1)
    launchd.install(
        render(tmp_path), home=tmp_path, launchctl=fake, sleep=sleeps.append
    )
    assert [c[0] for c in fake.calls].count("bootstrap") == 2 and fake.loaded
    assert sleeps == [1.0]


def test_install_reports_bootstrap_failure(tmp_path):
    with pytest.raises(launchd.LaunchdError, match="bootstrap exited 5"):
        launchd.install(
            render(tmp_path),
            home=tmp_path,
            launchctl=FakeLaunchctl(bootstrap_failures=2),
            sleep=lambda s: None,
        )


def test_status_extracts_state_lines_only():
    out = "gui/501/x = {\n\tstate = running\n\tpid = 42\n\tpath = /p\n}\n"
    assert launchd.status(launchctl=FakeLaunchctl(loaded=True, stdout=out)) == (
        "state = running\npid = 42"
    )
    assert launchd.status(launchctl=FakeLaunchctl()) == "not loaded"


def test_cli_install_print_and_uninstall(tmp_path, monkeypatch, fake_launchctl):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = write_config(tmp_path)
    runner = CliRunner()
    printed = runner.invoke(cli, ["install-agent", "--config", str(config), "--print"])
    assert printed.exit_code == 0, printed.output
    job = plistlib.loads(printed.stdout.encode())
    assert job["ProgramArguments"][0] == str(Path(sys.executable).absolute())
    assert job["ProgramArguments"][-1] == str(config.resolve())
    assert fake_launchctl.calls == []

    # No key file: launchd has no shell environment, so the LLM is skipped.
    assert "LLM stage will be skipped (api_key_env_unset)" in printed.stderr
    installed = runner.invoke(cli, ["install-agent", "--config", str(config)])
    assert installed.exit_code == 0, installed.output
    assert [c[0] for c in fake_launchctl.calls] == ["print", "bootstrap"]
    removed = runner.invoke(cli, ["uninstall-agent"])
    assert removed.exit_code == 0 and "Removed" in removed.output


def test_cli_run_refuses_duplicate_instance(tmp_path, restore_logging):
    config = write_config(tmp_path)
    with InstanceLock(tmp_path / "state_root" / "weaver.lock"):
        result = CliRunner().invoke(cli, ["run", "--config", str(config)])
    assert result.exit_code == 3
    # The duplicate never opened stores under state_root.
    assert not (tmp_path / "state_root" / "runtime.sqlite3").exists()
    log = (tmp_path / "log_root" / "weaver.log").read_text()
    assert "another weaver instance" in log
    assert stat.S_IMODE((tmp_path / "log_root" / "weaver.log").stat().st_mode) == (
        0o600
    )


def test_offline_tick_archives_and_reports_llm_skipped(tmp_path, caplog):
    body = "합성 공유 텍스트: 아무 일정도 없는 인사".encode()
    raw = json.loads((ROOT / "schema" / "examples" / "Manifest.json").read_text())
    raw |= {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
    config = load_config(write_config(tmp_path), environ={})
    (config.courier.outbox / f"{CAP}.txt").write_bytes(body)
    (config.courier.outbox / f"{CAP}.json").write_text(json.dumps(raw))

    runtime = build_runtime(config, environ={})
    stop = threading.Event()
    waits = iter([False, True])
    runtime._wait = lambda _timeout: next(waits)  # two ticks, no real sleep
    with caplog.at_level(logging.DEBUG):
        runtime.run(stop)
    assert (config.courier.receipts / f"{CAP}.json").exists()
    status = json.loads((config.state_root / "runtime-status.json").read_text())
    assert status["llm"] == {"available": False, "reason": "api_key_env_unset"}
    assert sum(status["sources"].values()) == 1
    assert set(status["sources"]) <= {"done", "waiting_llm"}
    text = body.decode()
    assert all(text not in r.getMessage() for r in caplog.records)


def test_cli_install_with_private_key_file_has_no_llm_warning(
    tmp_path, monkeypatch, fake_launchctl
):
    monkeypatch.setenv("HOME", str(tmp_path))
    key = tmp_path / "anthropic.key"
    key.write_text("sk-test\n")
    key.chmod(0o600)
    config = write_config(tmp_path, f'[llm]\napi_key_file = "{key}"\n')
    result = CliRunner().invoke(
        cli, ["install-agent", "--config", str(config), "--print"]
    )
    assert result.exit_code == 0
    assert "Warning" not in result.stderr


def test_install_refuses_invalid_config_with_ex_config(tmp_path, fake_launchctl):
    (tmp_path / "config.toml").write_text('sync_root = "x"\n')
    result = CliRunner().invoke(
        cli, ["install-agent", "--config", str(tmp_path / "config.toml")]
    )
    assert result.exit_code == 78
    assert fake_launchctl.calls == []


def test_config_error_under_launchd_waits_before_exit(tmp_path, monkeypatch):
    import time

    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setenv("WEAVER_SUPERVISED", "1")
    (tmp_path / "config.toml").write_text("not toml [")
    result = CliRunner().invoke(cli, ["run", "--config", str(tmp_path / "config.toml")])
    assert result.exit_code == 78
    assert slept == [launchd.CONFIG_ERROR_HOLD_SECONDS]
    assert "invalid TOML" in result.stderr


def test_logging_attaches_only_to_weaver_loggers(tmp_path):
    from weaver.runtime.logs import configure_logging

    weaver = logging.getLogger("weaver")
    saved, hook = list(weaver.handlers), sys.excepthook
    try:
        path = configure_logging(tmp_path / "logs")
        logging.getLogger("httpx").warning("GET https://example.invalid")
        logging.getLogger("weaver.runtime").info("weaver line")
        for handler in weaver.handlers:
            handler.flush()
        text = path.read_text()
        assert "weaver line" in text and "example.invalid" not in text
        assert logging.getLogger("httpx").level == logging.WARNING
        assert not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in logging.getLogger().handlers
        )
    finally:
        for handler in list(weaver.handlers):
            weaver.removeHandler(handler)
            handler.close()
        for handler in saved:
            weaver.addHandler(handler)
        weaver.setLevel(logging.NOTSET)
        sys.excepthook = hook


def test_damaged_archive_row_does_not_block_others(tmp_path):
    import sqlite3

    config = load_config(write_config(tmp_path), environ={})
    for index, text in enumerate(("첫째", "둘째")):
        body = f"합성 텍스트 {text}".encode()
        cap = f"cap_01ARZ3NDEKTSV4RRFFQ69G5FA{index}"
        raw = json.loads((ROOT / "schema" / "examples" / "Manifest.json").read_text())
        raw |= {
            "capture_id": cap,
            "payload": f"{cap}.txt",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
        }
        (config.courier.outbox / f"{cap}.txt").write_bytes(body)
        (config.courier.outbox / f"{cap}.json").write_text(json.dumps(raw))
    runtime = build_runtime(config, environ={})
    runtime.scanner.scan_once()
    rows = runtime.feed.source_rows()
    assert len(rows) == 2
    db = sqlite3.connect(config.archive_root / "index.sqlite3")
    with db:
        db.execute(
            "UPDATE sources SET source_json = '{broken' WHERE source_id = ?",
            (rows[0][0],),
        )
    db.close()
    runtime._tick(threading.Event())
    status = json.loads((config.state_root / "runtime-status.json").read_text())
    assert status["failed"] == [
        {"source_id": rows[0][0], "error_class": "ValidationError"}
    ]
    assert sum(status["sources"].values()) == 2
    assert len(runtime.feed.source_rows(exclude=runtime.ledger.final_ids())) == 1
    assert status["sources"]["waiting_llm"] == 1
