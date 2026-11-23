"""Fast-forward-only sync of the company vault clone (never commits to it)."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DIVERGED = "clone diverged: weaver reference sync --reclone"


class ReferenceSyncError(RuntimeError):
    """Messages never contain the remote URL (it may embed a token)."""


@dataclass(frozen=True)
class Change:
    status: Literal["A", "M", "D"]
    path: str


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            check=True,
            capture_output=True,
            cwd=cwd,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (subprocess.CalledProcessError, OSError):
        raise ReferenceSyncError(f"git {args[0]} failed") from None
    return done.stdout.decode("utf-8", "surrogateescape")


def sync_clone(remote: str, branch: str, clone: Path) -> str:
    """Clone or fast-forward ``clone`` and return its HEAD commit."""
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _git(
            [
                "clone",
                "--no-tags",
                "--single-branch",
                "--branch",
                branch,
                "--",
                remote,
                str(clone),
            ]
        )
        return _git(["rev-parse", "HEAD"], cwd=clone).strip()
    _git(["fetch", "--prune", "origin", branch], cwd=clone)
    try:
        _git(["merge", "--ff-only", f"origin/{branch}"], cwd=clone)
    except ReferenceSyncError:
        raise ReferenceSyncError(DIVERGED) from None
    head = _git(["rev-parse", "HEAD"], cwd=clone).strip()
    # "Already up to date" also succeeds when the clone is ahead of origin.
    if head != _git(["rev-parse", f"origin/{branch}"], cwd=clone).strip():
        raise ReferenceSyncError(DIVERGED)
    return head


def changes(clone: Path, last: str | None, head: str) -> list[Change] | None:
    """Changed paths since ``last``; ``None`` asks for a full reindex."""
    if last is None:
        return None
    try:
        _git(["merge-base", "--is-ancestor", last, head], cwd=clone)
    except ReferenceSyncError:
        return None
    raw = _git(
        ["diff", "--name-status", "--no-renames", "-z", f"{last}..{head}"], cwd=clone
    )
    parts = raw.split("\0")
    result: list[Change] = []
    for status, path in zip(parts[0::2], parts[1::2], strict=False):
        kind: Literal["A", "M", "D"] = (
            "A" if status == "A" else "D" if status == "D" else "M"
        )
        result.append(
            Change(kind, os.fsdecode(path.encode("utf-8", "surrogateescape")))
        )
    return result
