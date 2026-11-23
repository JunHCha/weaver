"""Guards for a Wiki folder inside a git-tracked home vault.

A personal vault may auto-push to a remote, so Wiki writes (which can contain
call summaries) must not start unless git ignores the Wiki folder. The Wiki
store also records which vault and folder it serves; a changed location needs
an explicit ``weaver wiki-rebind`` instead of silently mixing two vaults.
"""

import shutil
import subprocess
from pathlib import Path

from weaver.config import ConfigError

STATE_FILES = (
    "wiki.git",
    "wiki.sqlite3",
    "wiki-compiler.sqlite3",
    "wiki-documents.sqlite3",
    "wiki-digest.sqlite3",
    "wiki-queries.sqlite3",
)


class VaultNotIgnored(ConfigError):
    """The vault is a git repository that does not ignore the Wiki folder."""


class WikiBindingChanged(ConfigError):
    """The Wiki state belongs to a different vault or folder."""


def check_vault_ignored(vault: Path, folder: str) -> None:
    """Raise ``VaultNotIgnored`` unless git ignores ``folder/`` in the vault.

    The vault may be nested inside a larger git repository, so git is always
    asked first; only a missing git binary falls back to reading .gitignore.
    """
    probe = f"{folder}/.weaver-probe.md"
    message = f"{vault}/.gitignore에 '{folder}/' 무시 규칙이 없어 Wiki 쓰기를 시작하지 않습니다"
    try:
        result = subprocess.run(
            ["git", "-C", str(vault), "check-ignore", "-q", "--no-index", "--", probe],
            capture_output=True,
        )
    except FileNotFoundError:
        _check_without_git(vault, folder, message)
        return
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise VaultNotIgnored(message)
    inside = subprocess.run(
        ["git", "-C", str(vault), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 and "true" not in inside.stdout:
        # git reports it is not a repository (or the vault does not exist yet).
        return
    raise VaultNotIgnored(message)


def _check_without_git(vault: Path, folder: str, message: str) -> None:
    """Best effort when the git binary is missing."""
    resolved = vault.resolve()
    for directory in (resolved, *resolved.parents):
        if (directory / ".git").exists():
            if directory == resolved and _gitignore_lists(vault, folder):
                return
            raise VaultNotIgnored(message)


def _gitignore_lists(vault: Path, folder: str) -> bool:
    name = folder.strip("/")
    accepted = {name, f"{name}/", f"/{name}", f"/{name}/", f"{name}/**", f"/{name}/**"}
    try:
        lines = (vault / ".gitignore").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    ignored = False
    for line in (raw.strip() for raw in lines):
        if line in accepted:
            ignored = True
        elif line.startswith("!") and line[1:] in accepted:
            ignored = False
    return ignored


def rebind(state_root: Path, backup: Path, vault: Path | None = None) -> list[str]:
    """Move Wiki state into a new ``backup`` directory; return moved names."""
    if vault is not None and backup.resolve().is_relative_to(vault.resolve()):
        raise ConfigError("backup directory must be outside the vault")
    if backup.exists():
        raise ConfigError("backup directory must not exist")
    if backup.resolve().is_relative_to(state_root.resolve()):
        raise ConfigError("backup directory must be outside the state root")
    moved: list[str] = []
    backup.mkdir(parents=True)
    for name in STATE_FILES:
        source = state_root / name
        if source.exists():
            shutil.move(str(source), str(backup / name))
            moved.append(name)
    return moved
