"""Offline contract tests: no credentials, network, models, or user data."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from weaver.config import LlmConfig
from weaver.llm import (
    AuthenticationError,
    BudgetExceeded,
    InvalidJson,
    LlmError,
    LlmTimeout,
    MissingOutput,
    Refusal,
    SchemaMismatch,
    UnsupportedBackend,
    create_client,
)
from weaver.llm.client import AnthropicClient
from weaver.llm.ledger import Ledger
from weaver.llm.types import BackendReply, Request, TokenUsage


class Answer(BaseModel):
    count: int


class FakeTransport:
    def __init__(self, *replies: BackendReply | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[Request] = []

    async def send(self, request: Request, api_key: str) -> BackendReply:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_client(tmp_path: Path, *replies: BackendReply | Exception):
    transport = FakeTransport(*replies)
    ledger = Ledger(tmp_path / "llm.sqlite3")
    client = AnthropicClient(
        LlmConfig(api_key_env="TEST_KEY"),
        ledger,
        transport=transport,
        environ={"TEST_KEY": "secret-key"},
    )
    return client, transport, ledger


def test_contract_and_request(tmp_path: Path) -> None:
    usage = TokenUsage(input_tokens=100, output_tokens=20)
    client, transport, ledger = make_client(
        tmp_path,
        BackendReply('{"count": 3}', usage=usage),
        BackendReply("result", usage=usage),
    )
    assert isinstance(
        client.generate_json("loom.extract", "private-system", "private-user", Answer),
        Answer,
    )
    assert (
        client.generate_text("spinner.revise", "private-system", "private-user")
        == "result"
    )
    request = transport.requests[0]
    assert request.model == "claude-sonnet-5-5"
    assert request.json_schema is not None
    assert request.max_tokens > 0
    rows = ledger.records()
    assert len(rows) == 2
    assert rows[0]["cost_source"] == "usage_calculated"
    assert rows[0]["cost_usd"] == pytest.approx(0.0004)
    assert rows[0]["attempts"] == 1
    assert rows[0]["elapsed_seconds"] >= 0
    recorded = json.dumps(rows)
    for secret in ("private-system", "private-user", "secret-key"):
        assert secret not in recorded


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (BackendReply("{"), InvalidJson),
        (BackendReply('{"count":"bad"}'), SchemaMismatch),
        (BackendReply(None), MissingOutput),
        (BackendReply("no", stop_reason="refusal"), Refusal),
        (BackendReply("partial", stop_reason="max_tokens"), MissingOutput),
        (AuthenticationError(), AuthenticationError),
        (LlmTimeout(), LlmTimeout),
    ],
)
def test_failure_contract(tmp_path: Path, reply, error) -> None:
    client, transport, ledger = make_client(tmp_path, reply)
    with pytest.raises(error):
        client.generate_json("loom.extract", "system", "user", Answer)
    assert len(transport.requests) == 1
    row = ledger.records()[0]
    assert row["status"] == error.code
    assert row["cost_usd"] is None
    assert row["cost_source"] == "unavailable"


def test_missing_key_recorded_without_dispatch(tmp_path: Path) -> None:
    client, transport, ledger = make_client(tmp_path)
    client.environ = {}
    with pytest.raises(AuthenticationError):
        client.generate_text("spinner.revise", "system", "user")
    assert not transport.requests
    assert ledger.records()[0]["attempts"] == 0


@pytest.mark.parametrize("backend", ["none", "agent_sdk", "openai_compatible"])
def test_no_fallback(tmp_path: Path, backend) -> None:
    with pytest.raises(UnsupportedBackend):
        create_client(LlmConfig(backend=backend), state_root=tmp_path)


def test_total_deadline_cancels_transport(tmp_path: Path, monkeypatch) -> None:
    # Isolate network cancellation from real fullfsync reservation latency.
    # The separate preflight-deadline test covers elapsed preparation time.
    monkeypatch.setattr(
        "weaver.llm.client.time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    cancelled = []

    class Slow:
        async def send(self, request, api_key):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

    client, _, ledger = make_client(tmp_path)
    client.transport = Slow()
    client.deadline_seconds = 0.01
    with pytest.raises(LlmTimeout):
        client.generate_text("spinner.revise", "system", "user")
    assert cancelled == [True]
    assert ledger.records()[0]["attempts"] == 1


def test_json_capability_fail_closed(tmp_path: Path) -> None:
    client, transport, ledger = make_client(tmp_path)
    client.structured_output = False
    with pytest.raises(LlmError):
        client.generate_json("loom.extract", "system", "user", Answer)
    assert not transport.requests
    assert ledger.records()[0]["attempts"] == 0


def test_budget_blocks_before_transport(tmp_path: Path) -> None:
    client, transport, ledger = make_client(tmp_path, BackendReply("ok"))
    ledger.stop_usd = 0.01
    with pytest.raises(BudgetExceeded):
        client.generate_text("spinner.revise", "system", "user")
    assert not transport.requests
    assert ledger.records()[0]["status"] == "budget_exceeded"


def test_zero_reported_usage_is_distinct_from_missing(tmp_path: Path) -> None:
    client, _, ledger = make_client(
        tmp_path, BackendReply("ok", usage=TokenUsage(0, 0))
    )
    assert client.generate_text("spinner.revise", "s", "u") == "ok"
    row = ledger.records()[0]
    assert row["cost_usd"] == 0
    assert row["cost_source"] == "usage_calculated"
    assert row["budget_held_usd"] == 0


def test_unrepresentable_schema_is_redacted_and_recorded(tmp_path: Path) -> None:
    from pydantic import ConfigDict

    from weaver.llm import UnsupportedStructuredOutput

    class Private:
        pass

    class Unrepresentable(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        value: Private

    client, transport, ledger = make_client(tmp_path)
    with pytest.raises(UnsupportedStructuredOutput) as caught:
        client.generate_json("loom.extract", "s", "u", Unrepresentable)
    assert str(caught.value) == "unsupported_structured_output"
    assert not transport.requests
    assert ledger.records()[0]["status"] == "unsupported_structured_output"
    assert ledger.records()[0]["attempts"] == 0


def test_validation_diagnostic_survives_redacted_api_error(tmp_path, caplog):
    class Response(BaseModel):
        direction: str

    client, _, _ = make_client(
        tmp_path, BackendReply('{"direction":{"synthetic-private-source":"private"}}')
    )
    with pytest.raises(SchemaMismatch):
        client.generate_json("loom.approve", "system", "prompt", Response)
    assert "ValidationError reason=" in caplog.text
    assert '"loc": ["direction"]' in caplog.text
    assert "synthetic-private-source" not in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "reply,error",
    [
        (BackendReply("hello", usage=TokenUsage(3400, 800)), None),
        (AuthenticationError(), AuthenticationError),
    ],
)
def test_api_call_summary_is_one_redacted_info_line(tmp_path, caplog, reply, error):
    import logging

    client, _, _ = make_client(tmp_path, reply)
    with caplog.at_level(logging.INFO):
        if error:
            with pytest.raises(error):
                client.generate_text("test.call", "private-system", "private-user")
        else:
            assert (
                client.generate_text("test.call", "private-system", "private-user")
                == "hello"
            )
    lines = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 1
    assert "test.call anthropic" in lines[0]
    assert "(today 0/-)" in lines[0]
    assert not any(
        s in caplog.text
        for s in ("private-system", "private-user", "secret-key", "hello")
    )
