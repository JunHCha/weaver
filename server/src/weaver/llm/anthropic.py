"""Anthropic Client SDK adapter. No tools, sessions, fallback or SDK retries."""

import anthropic

from .errors import (
    AuthenticationError,
    LlmError,
    LlmTimeout,
    RateLimited,
    TransientError,
)
from .types import BackendReply, Request, TokenUsage


class AnthropicTransport:
    async def send(self, request: Request, api_key: str) -> BackendReply:
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
            raise LlmError() from None
        except (anthropic.APIError, ValueError, TypeError):
            raise LlmError() from None
