"""Anthropic Client SDK adapter. No tools, sessions, fallback or SDK retries."""

import logging

import anthropic

from .errors import (
    AuthenticationError,
    InsufficientCredit,
    LlmError,
    LlmTimeout,
    RateLimited,
    TransientError,
    UnbilledRejection,
)
from .types import BackendReply, Request, TokenUsage


class _SuppressTransportLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # SDK Request options and exception traces can include the complete
        # credential, prompt and response, even at error severity.
        return False


_PRIVATE_LOG_FILTER = _SuppressTransportLogs()
_PRIVATE_LOG_NAMES = ("anthropic", "httpx", "httpcore")


def _silence_transport_logs() -> None:
    # Reassert before EVERY request, after SDK import / ANTHROPIC_LOG setup.
    # Parent.disabled does not apply to descendants, so include existing child
    # loggers too. New children inherit the parents' above-CRITICAL level.
    # Keep this process-wide: restoring levels would expose concurrent calls.
    names = {*_PRIVATE_LOG_NAMES, *tuple(logging.Logger.manager.loggerDict)}
    for name in names:
        if any(
            name == root or name.startswith(root + ".") for root in _PRIVATE_LOG_NAMES
        ):
            logger = logging.getLogger(name)
            logger.disabled = True
            logger.setLevel(logging.CRITICAL + 1)
            # Retain suppression if a later logging config re-enables an
            # existing logger or ANTHROPIC_LOG resets its level.
            if _PRIVATE_LOG_FILTER not in logger.filters:
                logger.addFilter(_PRIVATE_LOG_FILTER)


def _is_credit_exhausted(message: object) -> bool:
    return isinstance(message, str) and "credit balance" in message.lower()


class AnthropicTransport:
    async def send(self, request: Request, api_key: str) -> BackendReply:
        _silence_transport_logs()
        try:
            # Explicit endpoint and key prevent ambient SDK environment overrides.
            async with anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url="https://api.anthropic.com",
                max_retries=0,
            ) as sdk:
                output: anthropic.types.OutputConfigParam = {"effort": "high"}
                if request.json_schema is not None:
                    output["format"] = {
                        "type": "json_schema",
                        "schema": request.json_schema,
                    }
                # Remove all case variants: the SDK merges ordinary header
                # dictionaries before HTTPX normalizes names, so overriding a
                # single spelling can accidentally send TWO API key values.
                headers: dict[str, str | anthropic.Omit] = {
                    name: anthropic.omit
                    for name in sdk.default_headers
                    if name.lower() in {"x-api-key", "authorization"}
                }
                headers["X-Api-Key"] = api_key
                response = await sdk.messages.create(
                    model=request.model,
                    system=request.system,
                    messages=[{"role": "user", "content": request.user}],
                    max_tokens=request.max_tokens,
                    # SDK 0.125 lacks this new enum in its generated types.
                    # Use the documented API field without lying to the type checker.
                    extra_body={"thinking": {"type": "between_tools"}},
                    output_config=output,
                    service_tier="standard_only",
                    # Custom SDK environment headers must not replace the
                    # credential selected by llm.api_key_env.
                    extra_headers=headers,
                )
                # No provider response text is logged or placed in exceptions.
                texts = [b.text for b in response.content if b.type == "text"]
                usage = response.usage
                parsed_usage = None
                if usage is not None:
                    cache = usage.cache_creation
                    creation = usage.cache_creation_input_tokens or 0
                    # Unknown TTL cannot be underpriced: use the higher 1h rate.
                    five = cache.ephemeral_5m_input_tokens if cache else 0
                    hour = cache.ephemeral_1h_input_tokens if cache else creation
                    parsed_usage = TokenUsage(
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_read_input_tokens or 0,
                        five,
                        hour,
                    )
                return BackendReply(
                    "".join(texts) if texts else None,
                    response.stop_reason,
                    parsed_usage,
                )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise AuthenticationError() from None
        except anthropic.RateLimitError:
            raise RateLimited() from None
        except anthropic.APITimeoutError:
            raise LlmTimeout() from None
        except anthropic.APIStatusError as exc:
            if exc.status_code in {500, 502, 503, 504, 529}:
                raise TransientError() from None
            # The message is used only to classify; it is never kept or logged.
            if exc.status_code == 400 and _is_credit_exhausted(exc.message):
                raise InsufficientCredit() from None
            if 400 <= exc.status_code < 500:
                # Rejected before processing: certainly not billed.
                raise UnbilledRejection() from None
            raise LlmError() from None
        except (anthropic.APIError, ValueError, TypeError):
            raise LlmError() from None
