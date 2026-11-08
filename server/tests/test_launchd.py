"""LaunchAgent plist, fake launchctl, CLI wiring and an offline end-to-end tick."""

import hashlib
import json
import logging
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
    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode, self.stdout = returncode, stdout

    def __call__(self, args):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


@pytest.fixture
def fake_launchctl(monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "run_launchctl", fake)
    return fake


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
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
    assert job["ExitTimeOut"] == 60
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
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    target = launchd.install(render(tmp_path), home=tmp_path, launchctl=fake)
    assert target == tmp_path / "Library/LaunchAgents/io.github.junhcha.weaver.plist"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert fake.calls == [
        ["bootout", "gui/501/io.github.junhcha.weaver"],
        ["bootstrap", "gui/501", str(target)],
    ]
    assert launchd.uninstall(home=tmp_path, launchctl=fake) is True
    assert not target.exists()
    assert fake.calls[-1] == ["bootout", "gui/501/io.github.junhcha.weaver"]
    assert launchd.uninstall(home=tmp_path, launchctl=fake) is False


def test_install_reports_bootstrap_failure(tmp_path):
    with pytest.raises(launchd.LaunchdError, match="bootstrap exited 5"):
        launchd.install(
            render(tmp_path), home=tmp_path, launchctl=FakeLaunchctl(returncode=5)
        )


def test_status_extracts_state_lines_only():
    out = "gui/501/x = {\n\tstate = running\n\tpid = 42\n\tpath = /p\n}\n"
    assert launchd.status(launchctl=FakeLaunchctl(stdout=out)) == (
        "state = running\npid = 42"
    )
    assert launchd.status(launchctl=FakeLaunchctl(returncode=113)) == "not loaded"


def test_cli_install_print_and_uninstall(tmp_path, monkeypatch, fake_launchctl):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = write_config(tmp_path)
    runner = CliRunner()
    printed = runner.invoke(cli, ["install-agent", "--config", str(config), "--print"])
    assert printed.exit_code == 0, printed.output
    job = plistlib.loads(printed.output.encode())
    assert job["ProgramArguments"][0] == str(Path(sys.executable).absolute())
    assert job["ProgramArguments"][-1] == str(config.resolve())
    assert fake_launchctl.calls == []

    installed = runner.invoke(cli, ["install-agent", "--config", str(config)])
    assert installed.exit_code == 0, installed.output
    assert [c[0] for c in fake_launchctl.calls] == ["bootout", "bootstrap"]
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
    # #27 store is the FeedbackSink/EntityIndex source and is purged periodically.
    assert (config.state_root / "feedback.sqlite3").exists()
    assert [task.name for task in runtime.maintenance] == ["feedback_purge"]
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
