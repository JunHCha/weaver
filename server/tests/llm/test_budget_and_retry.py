"""Money and retry invariants survive restarts and concurrent callers."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from weaver.config import LlmConfig
from weaver.llm import AuthenticationError, BudgetExceeded, LlmError, LlmTimeout
from weaver.llm.client import AnthropicClient
from weaver.llm.errors import (
    InsufficientCredit,
    RateLimited,
    SchemaMismatch,
    TransientError,
    UnbilledRejection,
)
from weaver.llm.ledger import NANO_USD, Ledger
from weaver.llm.types import BackendReply, TokenUsage

from .test_client import make_client


@pytest.mark.parametrize("error", [RateLimited, TransientError])
def test_only_one_retry_and_failed_attempts_recorded(tmp_path: Path, error) -> None:
    client, transport, ledger = make_client(
        tmp_path, error(), error(), BackendReply("never")
    )
    with pytest.raises(error):
        client.generate_text("spinner.revise", "system", "user")
    assert len(transport.requests) == 2
    row = ledger.records()[0]
    assert row["attempts"] == 2
    assert row["status"] == error.code
    assert len(row["attempt_records"]) == 2
    assert row["cost_usd"] is None
    if error is TransientError:
        assert row["budget_held_usd"] > 0
    else:
        assert row["budget_held_usd"] == 0


@pytest.mark.parametrize("error", [RateLimited, TransientError])
def test_success_after_retry_keeps_unknown_cost_separate(tmp_path: Path, error) -> None:
    client, _, ledger = make_client(
        tmp_path, error(), BackendReply("ok", usage=TokenUsage(100, 20))
    )
    assert client.generate_text("spinner.revise", "s", "u") == "ok"
    row = ledger.records()[0]
    assert row["attempts"] == 2
    assert row["attempt_records"][1]["cost_usd"] == 0.0004
    if error is TransientError:
        assert row["cost_usd"] is None
        assert row["budget_held_usd"] > 0.0004
    else:
        assert row["cost_usd"] == 0.0004
        assert row["budget_held_usd"] == 0.0004


@pytest.mark.parametrize("error", [AuthenticationError, LlmTimeout, LlmError])
def test_terminal_errors_are_not_retried(tmp_path: Path, error) -> None:
    client, transport, ledger = make_client(tmp_path, error(), BackendReply("never"))
    with pytest.raises(error):
        client.generate_text("spinner.revise", "s", "u")
    assert len(transport.requests) == 1
    assert ledger.records()[0]["attempts"] == 1


def test_timeout_keeps_one_attempt_reservation_after_restart(tmp_path: Path) -> None:
    client, _, ledger = make_client(tmp_path, LlmTimeout())
    with pytest.raises(LlmTimeout):
        client.generate_text("spinner.revise", "s", "u")
    row = Ledger(ledger.path).records()[0]
    assert row["budget_held_usd"] > 0
    assert row["cost_usd"] is None
    assert row["cost_source"] == "unavailable"
    # A later call cannot spend the money reserved for the unknown timeout.
    ledger.stop_usd = row["budget_held_usd"]
    with pytest.raises(BudgetExceeded):
        client.generate_text("spinner.revise", "s", "u")


def test_deadline_includes_retry_backoff(tmp_path: Path, monkeypatch) -> None:
    # Count the actual asyncio backoff, independent of fullfsync disk latency.
    monkeypatch.setattr(
        "weaver.llm.client.time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    client, transport, ledger = make_client(
        tmp_path, RateLimited(), BackendReply("never")
    )
    client.deadline_seconds = 0.01
    started = time.monotonic()
    with pytest.raises(LlmTimeout):
        client.generate_text("spinner.revise", "s", "u")
    assert time.monotonic() - started < 1
    assert len(transport.requests) == 1
    assert ledger.records()[0]["budget_held_usd"] == 0


def test_atomic_budget_reservations(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "llm.sqlite3", warn_usd=0.8, stop_usd=1)

    def reserve():
        try:
            return ledger.reserve({"status": "pending"}, 600_000_000)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(item is not None for item in results) == 1
    rows = Ledger(ledger.path).records()
    assert sum(row["budget_held_usd"] for row in rows) == 0.6
    assert sum(row["status"] == "budget_exceeded" for row in rows) == 1


def test_warning_and_unknown_crash_reservation_survive_restart(
    tmp_path: Path, caplog
) -> None:
    ledger = Ledger(tmp_path / "llm.sqlite3", warn_usd=0.5, stop_usd=1)
    ledger.reserve({"status": "pending"}, 600_000_000)
    assert "monthly budget warning" in caplog.text
    reopened = Ledger(ledger.path, warn_usd=0.5, stop_usd=1)
    with pytest.raises(BudgetExceeded):
        reopened.reserve({"status": "pending"}, 600_000_000)


def test_month_rollover_and_idempotent_settlement(tmp_path: Path, monkeypatch) -> None:
    import weaver.llm.ledger as module

    class Clock(datetime):
        month_value = 1

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, cls.month_value, 1, tzinfo=UTC)

    monkeypatch.setattr(module, "datetime", Clock)
    ledger = Ledger(tmp_path / "llm.sqlite3", warn_usd=0.8, stop_usd=1)
    call_id = ledger.reserve({"status": "pending"}, NANO_USD)
    with pytest.raises(BudgetExceeded):
        ledger.reserve({"status": "pending"}, 1)
    ledger.finish(call_id, {"status": "succeeded"}, 100)
    ledger.finish(call_id, {"status": "succeeded"}, 100)
    ledger.reserve({"status": "pending"}, NANO_USD - 100)
    Clock.month_value = 2
    ledger.reserve({"status": "pending"}, NANO_USD)


def test_schema_original_constraints_still_validated(tmp_path: Path) -> None:
    class Constrained(BaseModel):
        count: int = Field(ge=10)

    client, transport, ledger = make_client(
        tmp_path, BackendReply('{"count": 2}', usage=TokenUsage(10, 5))
    )
    with pytest.raises(SchemaMismatch):
        client.generate_json("loom.extract", "s", "u", Constrained)
    schema = transport.requests[0].json_schema
    assert "minimum" not in schema["properties"]["count"]
    assert ledger.records()[0]["cost_source"] == "usage_calculated"
    assert ledger.records()[0]["budget_held_usd"] == 0.00007


def test_sync_protocol_works_inside_existing_event_loop(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path, BackendReply("ok"))

    async def run():
        return client.generate_text("spinner.revise", "s", "u")

    assert asyncio.run(run()) == "ok"


def test_env_reference_resolved_each_call(tmp_path: Path) -> None:
    env = {}

    class Fake:
        async def send(self, request, api_key):
            assert api_key == "new-key"
            return BackendReply("ok")

    ledger = Ledger(tmp_path / "llm.sqlite3")
    client = AnthropicClient(
        LlmConfig(api_key_env="CUSTOM_KEY"), ledger, environ=env, transport=Fake()
    )
    with pytest.raises(AuthenticationError):
        client.generate_text("spinner.revise", "s", "u")
    env["CUSTOM_KEY"] = "new-key"
    assert client.generate_text("spinner.revise", "s", "u") == "ok"


def test_preparation_consumes_deadline_without_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    client, transport, ledger = make_client(tmp_path, BackendReply("never"))
    original = ledger.reserve

    def slow_reserve(record, amount):
        result = original(record, amount)
        time.sleep(0.02)
        return result

    monkeypatch.setattr(ledger, "reserve", slow_reserve)
    client.deadline_seconds = 0.01
    with pytest.raises(LlmTimeout):
        client.generate_text("spinner.revise", "s", "u")
    assert not transport.requests
    row = ledger.records()[0]
    assert row["attempts"] == 0
    assert row["budget_held_usd"] == 0


def _held(ledger: Ledger) -> int:
    month = datetime.now(UTC).strftime("%Y-%m")
    with ledger._connect() as db:
        return ledger._reservation_total(db, month)


@pytest.mark.parametrize("error", [InsufficientCredit, UnbilledRejection])
def test_unbilled_rejection_settles_zero_and_is_not_retried(
    tmp_path: Path, error
) -> None:
    client, transport, ledger = make_client(tmp_path, error(), BackendReply("never"))
    with pytest.raises(error):
        client.generate_text("spinner.revise", "s", "u")
    assert len(transport.requests) == 1
    row = ledger.records()[0]
    assert row["status"] == error.code
    assert row["budget_held_usd"] == 0
    assert row["attempt_records"][0]["status"] == error.code
    assert _held(ledger) == 0


def test_rejected_calls_do_not_consume_monthly_budget(tmp_path: Path) -> None:
    client, _, ledger = make_client(tmp_path, *[InsufficientCredit() for _ in range(5)])
    ledger.stop_usd = 0.5  # a single held reservation would exceed this
    for _ in range(5):
        with pytest.raises(InsufficientCredit):
            client.generate_text("spinner.revise", "s", "u")
    assert all(r["budget_held_usd"] == 0 for r in ledger.records())


@pytest.mark.parametrize("error", [LlmTimeout, TransientError, LlmError])
def test_uncertain_failures_keep_reservation(tmp_path: Path, error) -> None:
    client, _, ledger = make_client(tmp_path, error(), error())
    with pytest.raises(error):
        client.generate_text("spinner.revise", "s", "u")
    assert ledger.records()[0]["budget_held_usd"] > 0


def test_concurrent_rejections_settle_atomically(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "llm.sqlite3")
    ids = [ledger.reserve({"status": "pending"}, 100_000_000) for _ in range(8)]

    def settle(call_id: str) -> None:
        ledger.finish(call_id, {"status": "insufficient_credit"}, 0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(settle, ids))
    rows = ledger.records()
    assert len(rows) == 8
    assert all(r["budget_held_usd"] == 0 for r in rows)
    assert _held(ledger) == 0
