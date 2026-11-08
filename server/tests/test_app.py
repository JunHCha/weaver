"""Verify the installed entry point without models, network, or user data."""

import os
import subprocess
import sys
from pathlib import Path


def run_isolated(tmp_path: Path, code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("WEAVER_CONFIG", None)
    env.pop("XDG_CONFIG_HOME", None)
    # Typer forces Rich terminal styling under these (e.g. on GitHub Actions),
    # which splits help text with ANSI codes; the output here is a captured pipe.
    for name in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
        env.pop(name, None)
    env["HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


# An audit hook runs before application import and rejects external work. Read-only
# Python/native code imports, including installed dependencies, remain available.
# Data/model/config file reads remain forbidden.
GUARD = """
import sys
import threading
import os
import importlib.machinery
import sysconfig

package = importlib.machinery.PathFinder.find_spec("weaver")
code_roots = [
    os.path.realpath(sys.base_prefix),
    os.path.realpath(sysconfig.get_path("purelib")),
    *package.submodule_search_locations,
]

def reject_thread(*args, **kwargs):
    raise AssertionError("application started a thread")

threading.Thread.start = reject_thread

def guard(event, args):
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")):
        raise AssertionError(f"external operation: {event}")
    if event in {"os.system", "os.mkdir", "os.rename", "os.remove", "os.rmdir",
                 "os.chdir"}:
        raise AssertionError(f"filesystem operation: {event}")
    if event in {"os.listdir", "os.scandir"}:
        path = args[0]
        if not isinstance(path, str) or not any(
            path == root or path.startswith(root + "/") for root in code_roots
        ):
            raise AssertionError(f"directory access: {path}")
    if event == "open":
        path, mode, flags = args
        if not isinstance(path, str) or not path.endswith((".py", ".pyc", ".so")):
            raise AssertionError(f"data access: {path}")
        if mode is not None and any(flag in mode for flag in "wax+"):
            raise AssertionError(f"file write: {path}")

sys.addaudithook(guard)
"""


def test_installed_package_and_boundaries(tmp_path: Path) -> None:
    result = run_isolated(
        tmp_path,
        "import importlib\n"
        "import weaver\n"
        "for name in ('schema', 'courier', 'archive', 'spinner', 'llm', "
        "'loom', 'fabric', 'publish', 'alert'):\n"
        "    importlib.import_module('weaver.' + name)\n",
    )
    assert result.returncode == 0, result.stderr


def test_import_has_no_side_effects(tmp_path: Path) -> None:
    result = run_isolated(
        tmp_path, GUARD + "\nimport weaver.app\nimport weaver.config\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_module_help_has_no_side_effects(tmp_path: Path) -> None:
    result = run_isolated(
        tmp_path,
        GUARD + '\nimport runpy\nsys.argv = ["weaver.app", "--help"]\n'
        'runpy.run_module("weaver.app", run_name="__main__")\n',
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "--help" in result.stdout
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_runtime_requires_valid_configuration(tmp_path: Path) -> None:
    result = run_isolated(
        tmp_path,
        'import runpy, sys\nsys.argv = ["weaver.app", "run"]\n'
        'runpy.run_module("weaver.app", run_name="__main__")\n',
    )
    assert result.returncode == 78  # EX_CONFIG
    assert "cannot read configuration" in result.stderr
