import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from weaver.config import ServerConfig, load_config

ROOTS = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
CHANNELS = ("outbox", "receipts", "proposals", "decisions")
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *IDENT, *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


class Origin:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir()
        git(path, "init", "-b", "main")

    def commit(self, files: dict[str, bytes | None]) -> str:
        for name, data in files.items():
            target = self.path / name
            if data is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-m", "c")
        return self.head()

    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path / "origin")


ConfigFactory = Callable[[str], tuple[ServerConfig, Path]]


@pytest.fixture
def make_config(tmp_path: Path) -> ConfigFactory:
    """Build a config; the argument is extra TOML appended after the roots."""

    def build(extra: str) -> tuple[ServerConfig, Path]:
        base = tmp_path / "cfg"
        base.mkdir(exist_ok=True)
        path = base / "config.toml"
        path.write_text("\n".join(f'{k} = "{k}"' for k in ROOTS) + "\n" + extra)
        for key in ROOTS:
            (base / key).mkdir(exist_ok=True)
        for channel in CHANNELS:
            (base / "sync_root" / channel).mkdir(exist_ok=True)
        return load_config(path, environ={}), path

    return build


def company_toml(origin: Origin) -> str:
    return (
        '[reference]\ndefault_sphere = "회사"\n[[reference.vault]]\n'
        f'sphere = "회사"\nsource = "git"\ngit_remote = "{origin.path}"\nbranch = "main"\n'
    )
