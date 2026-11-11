"""Foreground stderr selection, formatting and privacy with isolated handlers."""

import io
import logging
import sys

import pytest

from weaver.runtime.logs import configure_logging


@pytest.mark.parametrize(
    "tty,supervised,override,expected",
    [
        (True, False, None, True),
        (False, False, None, False),
        (True, True, None, False),
        (False, True, None, False),
        (False, True, True, True),
        (True, False, False, False),
    ],
)
def test_console_policy(tmp_path, monkeypatch, tty, supervised, override, expected):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: tty)
    monkeypatch.setattr(sys, "stderr", stream)
    if supervised:
        monkeypatch.setenv("WEAVER_SUPERVISED", "1")
    else:
        monkeypatch.delenv("WEAVER_SUPERVISED", raising=False)
    weaver = logging.getLogger("weaver")
    saved, level, hook = list(weaver.handlers), weaver.level, sys.excepthook
    # Keep pytest's/global caller's handlers alive across configuration.
    weaver.handlers = []
    try:
        path = configure_logging(tmp_path, console=override)
        # Reconfiguration must replace handlers, avoiding duplicated output.
        configure_logging(tmp_path, console=override)
        try:
            raise ValueError("synthetic-private-source")
        except ValueError:
            logging.getLogger("weaver.runtime").warning(
                "safe event", exc_info=True, stack_info=True
            )
        file_text = path.read_text()
        assert file_text.count("safe event") == 1
        assert "synthetic-private-source" not in file_text
        assert "Traceback" not in file_text and "Stack" not in file_text
        assert stream.getvalue() == (file_text if expected else "")
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        for handler in weaver.handlers:
            handler.close()
        weaver.handlers = saved
        weaver.setLevel(level)
        sys.excepthook = hook


def test_run_help_exposes_console_override():
    from typer.testing import CliRunner

    from weaver.app import cli

    result = CliRunner().invoke(cli, ["run", "--help"], color=False)
    assert result.exit_code == 0
    assert "console" in result.stdout and "no-console" in result.stdout


@pytest.mark.parametrize(
    "option,expected", [(None, None), ("--console", True), ("--no-console", False)]
)
def test_run_passes_console_override(tmp_path, monkeypatch, option, expected):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import weaver.app
    import weaver.runtime.assemble
    import weaver.runtime.logs

    settings = SimpleNamespace(log_root=tmp_path, state_root=tmp_path)
    monkeypatch.setattr(weaver.app, "_load", lambda *args, **kwargs: settings)
    calls = []

    def configure(path, *, console):
        calls.append(console)
        return path / "weaver.log"

    monkeypatch.setattr(weaver.runtime.logs, "configure_logging", configure)
    monkeypatch.setattr(
        weaver.runtime.assemble,
        "build_runtime",
        lambda *args, **kwargs: SimpleNamespace(serve=lambda **kwargs: 0),
    )
    result = CliRunner().invoke(weaver.app.cli, ["run"] + ([option] if option else []))
    assert result.exit_code == 0, result.exception
    assert calls == [expected]
