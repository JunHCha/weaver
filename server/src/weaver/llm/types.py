"""Backend-independent public contract and private transport values."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


class LlmClient(Protocol):
    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T: ...

    def generate_text(self, task: str, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_input_tokens,
                self.cache_creation_5m_tokens,
                self.cache_creation_1h_tokens,
            )
        ):
            raise ValueError("negative token usage")

    @property
    def cost_nano_usd(self) -> int:
        # ADR-021 price snapshot, 2026-11-07, standard global Sonnet 5.5.
        # Output is the inclusive billable count, including thinking.
        return (
            self.input_tokens * 2000
            + self.output_tokens * 10000
            + self.cache_read_input_tokens * 100
            + self.cache_creation_5m_tokens * 2500
            + self.cache_creation_1h_tokens * 4000
        )


@dataclass(frozen=True)
class Request:
    model: str
    system: str
    user: str
    max_tokens: int
    json_schema: dict[str, object] | None = None


@dataclass(frozen=True)
class BackendReply:
    text: str | None
    stop_reason: str | None = "end_turn"
    usage: TokenUsage | None = None


class Transport(Protocol):
    async def send(self, request: Request, api_key: str) -> BackendReply: ...
