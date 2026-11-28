"""Specifications run only inside isolated synthetic worlds, with network denied."""

import os
import socket
import subprocess
from pathlib import Path

import pytest
from server.tests.harness import World


@pytest.fixture
def world_factory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith(
            ("WEAVER_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_", "GIT_")
        ):
            monkeypatch.delenv(key)
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

    def forbidden(*args, **kwargs):
        pytest.fail("World attempted external network, provider or speech-model IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("weaver.runtime.assemble.create_client", forbidden)
    popen = subprocess.Popen

    def local_git_only(args, *positional, **kwargs):
        assert not kwargs.get("shell"), "World forbids shell subprocesses"
        assert isinstance(args, (list, tuple)) and Path(args[0]).name == "git", args
        assert not {"push", "fetch", "pull", "clone", "ls-remote"}.intersection(args)
        cwd = Path(kwargs.get("cwd") or Path.cwd()).resolve()
        targets = []
        targets.extend(
            Path(args[i + 1]).resolve()
            for i, arg in enumerate(args[:-1])
            if arg in {"-C", "--git-dir", "--work-tree"}
        )
        targets.extend(
            Path(arg.split("=", 1)[1]).resolve()
            for arg in args
            if arg.startswith("--git-dir=")
        )
        if args[1:3] == ["init", "--bare"]:
            targets.append(Path(args[3]).resolve())
        assert all(path.is_relative_to(tmp_path) for path in (targets or [cwd])), args
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git_only)
    worlds = []

    def create(*, transport="files"):
        world = World(tmp_path / f"world-{len(worlds)}", transport=transport)
        worlds.append(world)
        return world

    yield create
    for world in worlds:
        world.close()
