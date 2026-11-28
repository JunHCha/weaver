"""Recovery tests use only synthetic SQLite data under tmp_path."""

import json
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from weaver.app import cli
from weaver.archive import ArchiveStore
from weaver.config import load_config
from weaver.llm.errors import UsageLimited
from weaver.llm.ledger import Ledger
from weaver.runtime.gate import LlmGate, LlmStatus
from weaver.runtime.ledger import RuntimeLedger
from weaver.runtime.loop import EXIT_ALREADY_RUNNING, InstanceLock, Runtime
from weaver.runtime.source_retry import reset_targets, select_targets


@pytest.fixture
def data(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
    )
    settings = load_config(config, init_directories=True)
    archive = ArchiveStore(settings.archive_root)
    runtime = RuntimeLedger(settings.state_root / "runtime.sqlite3")
    with sqlite3.connect(settings.archive_root / "index.sqlite3") as db:
        for i in range(40):
            sid = f"src_{i:03}"
            db.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?)",
                (
                    sid,
                    str(i),
                    "unused",
                    json.dumps(
                        {
                            "source_id": sid,
                            "captured_at": f"2026-11-{i // 10 + 24:02}T00:00:00Z",
                        }
                    ),
                ),
            )
            if i < 39:
                runtime.record(
                    sid,
                    "done" if i == 38 else "failed",
                    attempts=20,
                    contract_failures=3,
                    timeout_failures=4,
                    next_attempt_at=99999999999,
                    error_class="RateLimited",
                )
    with sqlite3.connect(settings.state_root / "summary.sqlite3") as db:
        db.execute("CREATE TABLE packages (package TEXT)")
        db.execute(
            "INSERT INTO packages VALUES (?)", (json.dumps({"source_id": "src_037"}),)
        )
    return config, settings, archive, runtime


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


def invoke(data, *args, input=None):
    return CliRunner().invoke(
        cli, ["sources", "retry", "--config", str(data[0]), *args], input=input
    )


def test_preview_filters_limit_and_no_writes(data):
    _, settings, _, ledger = data
    databases = [
        settings.state_root / "runtime.sqlite3",
        settings.state_root / "summary.sqlite3",
        settings.archive_root / "index.sqlite3",
    ]
    before = [dump(path) for path in databases]
    result = invoke(data, "--limit", "10", "--error-class", "RateLimited")
    assert result.exit_code == 0, result.output
    assert "Eligible Sources: 10 (limit: 10)" in result.output
    assert "RateLimited: 10" in result.output
    assert "src_009" in result.output and "src_010" not in result.output
    assert before == [dump(path) for path in databases]
    assert ledger.counts() == {"done": 1, "failed": 38}
    result = invoke(
        data, "--since", "2026-11-25T00:00:00Z", "--until", "2026-11-26T00:00:00Z"
    )
    assert "Eligible Sources: 10" in result.output
    result = invoke(data, "--source-id", "src_012", "--source-id", "src_018")
    assert "Eligible Sources: 2" in result.output
    assert "Eligible Sources: 0" in invoke(data, "--status", "done").output
    assert "Eligible Sources: 0" in invoke(data, "--error-class", "Other").output


def test_apply_requires_confirmation_and_excludes_protected(data):
    _, settings, _, ledger = data
    assert invoke(data, "--apply", input="n\n").exit_code != 0
    assert invoke(data, "--apply").exit_code != 0
    assert ledger.counts() == {"done": 1, "failed": 38}
    result = invoke(data, "--apply", input="y\n")
    assert result.exit_code == 0, result.output
    assert "Reset 37 Sources" in result.output
    assert ledger.counts() == {"done": 1, "failed": 1, "retry": 37}
    assert all(
        p.attempts == p.contract_failures == p.timeout_failures == 0
        and p.next_attempt_at is None
        for p in ledger.progress().values()
    )
    with sqlite3.connect(settings.state_root / "runtime.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM sources WHERE error_class IS NULL"
            ).fetchone()[0]
            == 37
        )


def test_lock_refuses_apply_but_allows_preview(data):
    with InstanceLock(data[1].state_root / "weaver.lock"):
        result = invoke(data, "--apply", input="y\n")
        assert result.exit_code == EXIT_ALREADY_RUNNING
        assert "stop the Weaver server" in result.output
        assert "Reset these Sources" not in result.output
        assert invoke(data).exit_code == 0
    assert data[3].counts() == {"done": 1, "failed": 38}


def test_preview_sees_committed_live_wal_and_apply_limit(data):
    path = data[1].state_root / "runtime.sqlite3"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE sources SET error_class='Other' WHERE source_id='src_000'"
        )
        writer.commit()
        with InstanceLock(data[1].state_root / "weaver.lock"):
            result = invoke(data, "--error-class", "Other")
            assert result.exit_code == 0, result.output
            assert "Eligible Sources: 1" in result.output
    result = invoke(data, "--apply", "--limit", "10", input="y\n")
    assert result.exit_code == 0, result.output
    assert list(data[3].progress()) == [f"src_{i:03}" for i in range(10)]


def test_limit_follows_arrival_order_not_captured_at(data):
    _, settings, _, _ = data
    with sqlite3.connect(settings.archive_root / "index.sqlite3") as db:
        db.execute(
            "UPDATE sources SET source_json=? WHERE source_id='src_000'",
            (json.dumps({"captured_at": "2026-12-31T00:00:00Z"}),),
        )
    targets = select_targets(settings.state_root, settings.archive_root, limit=2)
    assert [t.source_id for t in targets] == ["src_000", "src_001"]


@pytest.mark.parametrize(
    "raw",
    ["not json", "{}", "[]", '{"captured_at": "2026-11-24T00:00:00"}'],
)
def test_damaged_archive_row_does_not_block_other_targets(data, raw):
    _, settings, _, ledger = data
    with sqlite3.connect(settings.archive_root / "index.sqlite3") as db:
        db.execute("UPDATE sources SET source_json=? WHERE source_id='src_000'", (raw,))
    result = invoke(data, "--apply", "--limit", "2", input="y\n")
    assert result.exit_code == 0, result.output
    assert "Skipped unreadable archive row: src_000" in result.output
    assert list(ledger.progress()) == ["src_001", "src_002"]


def test_reset_transaction_rolls_back_all_targets(data):
    _, settings, _, ledger = data
    targets = select_targets(settings.state_root, settings.archive_root)
    with sqlite3.connect(settings.state_root / "runtime.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER reject_reset BEFORE UPDATE ON sources "
            "WHEN NEW.source_id='src_010' BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    with InstanceLock(settings.state_root / "weaver.lock"):
        with pytest.raises(sqlite3.IntegrityError):
            reset_targets(settings.state_root, targets)
    assert ledger.counts() == {"done": 1, "failed": 38}


@pytest.mark.parametrize(
    "args",
    [
        ("--limit", "0"),
        ("--status", "unknown"),
        ("--since", "2026-11-24"),
        ("--since", "2026-11-26T00:00:00Z", "--until", "2026-11-25T00:00:00Z"),
    ],
)
def test_invalid_filters(data, args):
    assert invoke(data, *args).exit_code != 0


@pytest.mark.parametrize("calls_per_source", [1, 3])
def test_backlog_arrival_order_daily_cap_and_new_source(data, calls_per_source):
    _, settings, archive, ledger = data
    targets = select_targets(settings.state_root, settings.archive_root)
    with InstanceLock(settings.state_root / "weaver.lock"):
        assert reset_targets(settings.state_root, targets) == 37
    accounting = Ledger(settings.state_root / "llm.sqlite3")
    day = ["2026-11-28"]
    processed = []

    class Processor:
        def process(self, source, stop):
            for _ in range(calls_per_source):
                if not accounting.take_daily_call(day[0], 100):
                    raise UsageLimited()
            processed.append(source.source_id)
            return "done"

    runtime = Runtime(
        state_root=settings.state_root,
        scanner=SimpleNamespace(),
        sources=archive,
        decode=lambda raw: SimpleNamespace(**json.loads(raw)),
        processor=Processor(),
        ledger=ledger,
        gate=LlmGate(lambda: LlmStatus(True, "test")),
        interval=1,
    )
    runtime._process_sources(threading.Event())
    expected = [t.source_id for t in targets] + ["src_039"]
    assert processed == expected[: len(processed)]
    if calls_per_source == 1:
        assert processed == expected  # 37 recovered + new fit under 100
    else:
        assert len(processed) == 33  # multiple calls can postpone new arrivals
        assert not accounting.take_daily_call(day[0], 100)
        day[0] = "2026-11-29"
        runtime.gate.refresh()
        runtime._process_sources(threading.Event())
        assert processed == expected
