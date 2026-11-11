"""Deterministic retries: no paid clients or real ledger/vault access."""

import logging

import pytest

from weaver.config import ConfigError, CourierSettings
from weaver.courier import Courier

from .conftest import PKG, verdict_for, write_json


def setup_retry(paths, captures, revisions, decisions, *, attempts=3):
    clock = [0.0]
    revisions.latest[PKG] = 1
    path = write_json(paths.decisions / f"{PKG}.r1.json", verdict_for())
    courier = Courier(
        paths,
        captures,
        revisions,
        decisions,
        retry=CourierSettings(attempts, 10, 15),
        clock=lambda: clock[0],
    )
    return courier, path, clock


def test_backoff_cap_and_stop_bound_failure_records(
    paths, captures, revisions, decisions, caplog
):
    courier, path, clock = setup_retry(paths, captures, revisions, decisions)
    decisions.failures = 10000
    with caplog.at_level(logging.WARNING):
        courier.scan_once()
        clock[0] = 9
        assert courier.scan_once().problems[f"decisions/{path.name}"] == "apply_backoff"
        assert len(decisions.calls) == 1
        clock[0] = 10
        courier.scan_once()
        clock[0] = 24
        courier.scan_once()
        assert len(decisions.calls) == 2
        clock[0] = 25  # second delay is capped at 15, rather than 20
        courier.scan_once()
        for _ in range(1000):
            clock[0] += 3600
            report = courier.scan_once()
        assert report.problems[f"decisions/{path.name}"] == "apply_stopped"
    assert len(decisions.calls) == 3  # each apply could create one LLM ledger failure
    assert path.exists()
    assert caplog.text.count("courier apply retries stopped:") == 1


def test_changed_error_resets_consecutive_limit(paths, captures, revisions, decisions):
    courier, _, clock = setup_retry(paths, captures, revisions, decisions, attempts=2)
    messages = iter(["first", "second", "second"])
    calls = []

    def fail(verdict):
        calls.append(verdict)
        raise RuntimeError(next(messages))

    decisions.apply = fail
    courier.scan_once()
    clock[0] = 10
    courier.scan_once()  # same exception class, different error; count restarts
    clock[0] = 20
    courier.scan_once()
    clock[0] = 100
    courier.scan_once()
    assert len(calls) == 3


@pytest.mark.parametrize("new_revision", [False, True])
def test_new_verdict_can_retry_after_stop(
    paths, captures, revisions, decisions, new_revision
):
    courier, path, _ = setup_retry(paths, captures, revisions, decisions, attempts=1)
    decisions.failures = 1
    courier.scan_once()
    revision = 2 if new_revision else 1
    revisions.latest[PKG] = revision
    write_json(
        paths.decisions / f"{PKG}.r{revision}.json",
        verdict_for(revision=revision, action="reject"),
    )
    assert courier.scan_once().applied == [(PKG, revision)]
    if not new_revision:
        assert not path.exists()


def test_restart_recovers_three_existing_failed_verdicts(
    paths, captures, revisions, decisions
):
    courier, _, clock = setup_retry(paths, captures, revisions, decisions, attempts=1)
    for pid in (PKG + "b", PKG + "c"):
        revisions.latest[pid] = 1
        write_json(paths.decisions / f"{pid}.r1.json", verdict_for(package_id=pid))
    decisions.failures = 3
    courier.scan_once()
    clock[0] = 10000
    courier.scan_once()
    assert len(decisions.calls) == 3
    restarted = Courier(paths, captures, revisions, decisions)
    assert len(restarted.scan_once().applied) == 3
    assert not list(paths.decisions.iterdir())


def test_unconfirmed_application_is_also_bounded(paths, captures, revisions, decisions):
    courier, _, clock = setup_retry(paths, captures, revisions, decisions, attempts=1)
    decisions.confirm = False
    courier.scan_once()
    clock[0] = 10000
    courier.scan_once()
    assert len(decisions.calls) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"backoff_seconds": 0},
        {"backoff_seconds": float("nan")},
        {"max_backoff_seconds": -1},
        {"backoff_seconds": 100, "max_backoff_seconds": 10},
    ],
)
def test_retry_settings_reject_invalid_values(values):
    with pytest.raises(ConfigError, match="courier"):
        CourierSettings(**values)


def test_cli_failure_ledger_does_not_grow_after_stop(
    tmp_path,
    paths,
    captures,
    revisions,
    decisions,
):
    from weaver.config import LlmConfig
    from weaver.llm.cli import ClaudeCliClient, RunResult
    from weaver.llm.ledger import Ledger
    from weaver.loom.summary_review import ApprovedResponse

    binary = tmp_path / "fake-claude"
    binary.write_text("#!/bin/sh\nexit 1\n")
    binary.chmod(0o700)
    ledger = Ledger(tmp_path / "llm.sqlite3")
    runner_calls = []

    def runner(*args, **kwargs):
        runner_calls.append(args)
        return RunResult(1, b"", b"synthetic failure")

    client = ClaudeCliClient(
        LlmConfig("claude_cli", claude_path=binary),
        ledger,
        state_root=tmp_path,
        runner=runner,
        environ={},
    )
    courier, _, clock = setup_retry(paths, captures, revisions, decisions)

    def apply(verdict):
        client.generate_json(
            "loom.approve", "synthetic system", "synthetic input", ApprovedResponse
        )

    decisions.apply = apply
    for _ in range(100):
        courier.scan_once()
        clock[0] += 1000
    assert len(runner_calls) == 3  # real schema transform reached the injected runner
    assert len(ledger.records()) == 3
    assert {record["status"] for record in ledger.records()} == {"backend_error"}


def test_apply_validation_logs_safe_reason(
    paths, captures, revisions, decisions, caplog
):
    from pydantic import BaseModel

    class Model(BaseModel):
        direction: str

    courier, _, _ = setup_retry(paths, captures, revisions, decisions)

    def apply(verdict):
        Model.model_validate({"direction": {"synthetic-private-source": "secret"}})

    decisions.apply = apply
    with caplog.at_level(logging.WARNING):
        courier.scan_once()
    assert "ValidationError reason=" in caplog.text
    assert '"loc": ["direction"]' in caplog.text
    assert "Input should be a string" in caplog.text
    assert "synthetic-private-source" not in caplog.text
    assert "secret" not in caplog.text
