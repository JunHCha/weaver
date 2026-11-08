"""User LaunchAgent plist and launchctl install/uninstall/status.

The agent runs ``<python> -m weaver.app run --config <file>`` with absolute
paths only (no shell, no PATH, no current directory). ``<python>`` is the
``server/.venv/bin/python`` that ``uv sync --project server --locked``
prepares; its symlink is kept unresolved so the venv stays active.

Restart policy: KeepAlive restarts only after an unsuccessful exit (crash,
duplicate-instance exit 3, config error 2); ThrottleInterval spaces restarts
at least 30 s apart so repeated failures cannot storm. SIGTERM from
``launchctl bootout`` exits 0 within ExitTimeOut (60 s) before SIGKILL.
No credentials are written to the plist.
"""

import os
import plistlib
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from weaver.courier.atomic import write_bytes_atomic

LABEL = "io.github.junhcha.weaver"
THROTTLE_SECONDS = 30
EXIT_TIMEOUT_SECONDS = 60

type Launchctl = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_launchctl(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, check=False
    )


class LaunchdError(RuntimeError):
    """launchctl failed; carries only the subcommand and exit status."""


def plist_path(home: Path, label: str = LABEL) -> Path:
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def render_plist(
    *,
    python: Path,
    config_file: Path,
    log_root: Path,
    working_directory: Path,
    label: str = LABEL,
) -> bytes:
    for name, path in (
        ("python", python),
        ("config_file", config_file),
        ("log_root", log_root),
        ("working_directory", working_directory),
    ):
        if not path.is_absolute():
            raise ValueError(f"{name}: expected absolute path")
    job: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [
            str(python),
            "-m",
            "weaver.app",
            "run",
            "--config",
            str(config_file),
        ],
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_SECONDS,
        "ExitTimeOut": EXIT_TIMEOUT_SECONDS,
        "ProcessType": "Standard",
        "Umask": 0o077,
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(log_root / "launchd.out.log"),
        "StandardErrorPath": str(log_root / "launchd.err.log"),
    }
    return plistlib.dumps(job, fmt=plistlib.FMT_XML, sort_keys=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(
    plist: bytes,
    *,
    home: Path,
    launchctl: Launchctl = run_launchctl,
    label: str = LABEL,
) -> Path:
    """Write the plist (0644, as launchd requires) and (re)bootstrap it."""
    target = plist_path(home, label)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(target, plist)
    os.chmod(target, 0o644)
    # Replace a previously loaded job; "not loaded" is not an error here.
    launchctl(["bootout", f"{_domain()}/{label}"])
    result = launchctl(["bootstrap", _domain(), str(target)])
    if result.returncode != 0:
        raise LaunchdError(f"bootstrap exited {result.returncode}")
    return target


def uninstall(
    *, home: Path, launchctl: Launchctl = run_launchctl, label: str = LABEL
) -> bool:
    """Stop the job (SIGTERM) and remove its plist; logs and state are kept."""
    target = plist_path(home, label)
    launchctl(["bootout", f"{_domain()}/{label}"])
    if target.exists():
        target.unlink()
        return True
    return False


def status(*, launchctl: Launchctl = run_launchctl, label: str = LABEL) -> str:
    result = launchctl(["print", f"{_domain()}/{label}"])
    if result.returncode != 0:
        return "not loaded"
    keys = ("state =", "pid =", "last exit code =")
    lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(keys)
    ]
    return "\n".join(lines) or "loaded"
