"""Exercise the real SDK serializer/error mapping over a fake HTTP transport."""

import asyncio
import json

import anthropic
import httpx
import pytest

from weaver.llm.anthropic import AnthropicTransport
from weaver.llm.errors import (
    AuthenticationError,
    LlmError,
    LlmTimeout,
    RateLimited,
    TransientError,
)
from weaver.llm.types import Request


@pytest.fixture
def fake_sdk(monkeypatch):
    original = anthropic.AsyncAnthropic
    clients = []
    requests = []

    def install(handler):
        def handle(request):
            requests.append(request)
            return handler(request)

        def factory(**kwargs):
            assert kwargs["max_retries"] == 0
            assert kwargs["api_key"] == "fake-key"
            assert kwargs["base_url"] == "https://api.anthropic.com"
            http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            clients.append(http)
            return original(**kwargs, http_client=http)

        monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)
        return requests, clients

    return install


def message(**changes):
    result = {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": '{"count": 1}'}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    result.update(changes)
    return result


def test_real_sdk_wire_format_and_usage(fake_sdk):
    requests, clients = fake_sdk(lambda _: httpx.Response(200, json=message()))
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    reply = asyncio.run(
        AnthropicTransport().send(
            Request("claude-sonnet-5-5", "system", "user", 1234, schema), "fake-key"
        )
    )
    assert reply.text == '{"count": 1}'
    assert reply.usage.cost_nano_usd == 400000
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["thinking"] == {"type": "between_tools"}
    assert body["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": schema},
    }
    assert body["max_tokens"] == 1234
    assert body["service_tier"] == "standard_only"
    assert body["messages"] == [{"role": "user", "content": "user"}]
    assert "tools" not in body
    assert requests[0].headers["x-api-key"] == "fake-key"
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (429, RateLimited),
        (500, TransientError),
        (502, TransientError),
        (503, TransientError),
        (504, TransientError),
        (529, TransientError),
        (400, LlmError),
        (501, LlmError),
    ],
)
def test_http_errors_are_redacted_and_not_sdk_retried(fake_sdk, status, error):
    requests, clients = fake_sdk(
        lambda _: httpx.Response(
            status,
            json={
                "type": "error",
                "error": {"type": "api_error", "message": "secret-prompt-key"},
            },
        )
    )
    with pytest.raises(error) as caught:
        asyncio.run(
            AnthropicTransport().send(
                Request("claude-sonnet-5-5", "s", "u", 10), "fake-key"
            )
        )
    assert str(caught.value) == error.code
    assert len(requests) == 1
    assert all(client.is_closed for client in clients)


def test_transport_timeout_is_redacted_and_client_closed(fake_sdk):
    def handle(request):
        raise httpx.ReadTimeout("secret-prompt-key", request=request)

    requests, clients = fake_sdk(handle)
    with pytest.raises(LlmTimeout):
        asyncio.run(
            AnthropicTransport().send(
                Request("claude-sonnet-5-5", "s", "u", 10), "fake-key"
            )
        )
    assert len(requests) == 1
    assert all(client.is_closed for client in clients)


def test_cache_ttls_and_inclusive_output(fake_sdk):
    fake_sdk(
        lambda _: httpx.Response(
            200,
            json=message(
                usage={
                    "input_tokens": 10,
                    "output_tokens": 30,
                    "output_tokens_details": {"thinking_tokens": 20},
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 50,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 20,
                        "ephemeral_1h_input_tokens": 30,
                    },
                }
            ),
        )
    )
    reply = asyncio.run(
        AnthropicTransport().send(
            Request("claude-sonnet-5-5", "s", "u", 100), "fake-key"
        )
    )
    assert reply.usage.cost_nano_usd == 500000


def test_missing_provider_usage_is_not_zero(fake_sdk):
    fake_sdk(lambda _: httpx.Response(200, json=message(usage=None)))
    reply = asyncio.run(
        AnthropicTransport().send(
            Request("claude-sonnet-5-5", "s", "u", 100), "fake-key"
        )
    )
    assert reply.usage is None


def test_configured_key_wins_over_sdk_environment(fake_sdk, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unselected-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unselected-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unselected.invalid")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "x-api-key: wrong-key\nAUTHORIZATION: Bearer wrong-token",
    )
    requests, _ = fake_sdk(lambda _: httpx.Response(200, json=message()))
    asyncio.run(
        AnthropicTransport().send(
            Request("claude-sonnet-5-5", "s", "u", 100), "fake-key"
        )
    )
    assert requests[0].url.host == "api.anthropic.com"
    assert requests[0].headers["x-api-key"] == "fake-key"
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
def test_debug_sdk_logs_never_include_credentials_or_content(
    fake_sdk,
    monkeypatch,
    caplog,
    outcome,
):
    import logging

    from anthropic._utils._logs import setup_logging

    # Reproduce ANTHROPIC_LOG and application-level DEBUG configuration. Include
    # child loggers: disabling only a parent does not suppress propagated logs.
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    setup_logging()
    caplog.set_level(logging.DEBUG)
    names = (
        "anthropic",
        "anthropic._base_client",
        "httpx",
        "httpx._client",
        "httpcore",
        "httpcore.connection",
    )
    for name in names:
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "disabled", False)
        monkeypatch.setattr(logger, "level", logging.DEBUG)

    def handle(request):
        # Lower libraries can attach request/response content to errors at any
        # severity. Also exercise the SDK's actual DEBUG Request options log.
        # Reconfiguration during an in-flight request must not undo the
        # privacy filter, including exception logs after this handler fails.
        setup_logging()
        for name in names:
            logger = logging.getLogger(name)
            logger.disabled = False
            logger.setLevel(logging.DEBUG)
            for level in (
                logging.DEBUG,
                logging.INFO,
                logging.WARNING,
                logging.ERROR,
                logging.CRITICAL,
            ):
                logging.getLogger(name).log(
                    level, "fake-key private-system private-user private-response"
                )
        if outcome == "timeout":
            raise httpx.ReadTimeout("private-response", request=request)
        if outcome == "error":
            return httpx.Response(500, json={"error": {"message": "private-response"}})
        return httpx.Response(
            200, json=message(content=[{"type": "text", "text": "private-response"}])
        )

    fake_sdk(handle)
    if outcome == "success":
        asyncio.run(
            AnthropicTransport().send(
                Request("claude-sonnet-5-5", "private-system", "private-user", 100),
                "fake-key",
            )
        )
    else:
        with pytest.raises(LlmError):
            asyncio.run(
                AnthropicTransport().send(
                    Request("claude-sonnet-5-5", "private-system", "private-user", 100),
                    "fake-key",
                )
            )
    logging.getLogger("weaver.llm.test").warning("safe-metrics-marker")
    assert "safe-metrics-marker" in caplog.text
    for secret in ("fake-key", "private-system", "private-user", "private-response"):
        assert secret not in caplog.text
