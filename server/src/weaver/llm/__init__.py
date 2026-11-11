"""Shared synchronous LLM contract, redacted errors and configured factory."""

from pathlib import Path

from weaver.config import LlmConfig

from .cli import ClaudeCliClient
from .client import AnthropicClient
from .codex import CodexCliClient
from .errors import (
    AuthenticationError,
    BudgetExceeded,
    InsufficientCredit,
    InvalidJson,
    LlmError,
    LlmTimeout,
    MissingOutput,
    RateLimited,
    Refusal,
    SchemaMismatch,
    TransientError,
    UnsupportedBackend,
    UnsupportedStructuredOutput,
    UsageLimited,
)
from .ledger import Ledger
from .types import LlmClient

__all__ = [
    "LlmClient",
    "AnthropicClient",
    "ClaudeCliClient",
    "CodexCliClient",
    "create_client",
    "AuthenticationError",
    "BudgetExceeded",
    "InsufficientCredit",
    "InvalidJson",
    "LlmError",
    "LlmTimeout",
    "MissingOutput",
    "RateLimited",
    "Refusal",
    "SchemaMismatch",
    "TransientError",
    "UnsupportedBackend",
    "UnsupportedStructuredOutput",
    "UsageLimited",
]


def create_client(config: LlmConfig, *, state_root: Path) -> LlmClient:
    """Construct without credentials/network; resolve api_key_env per call.

    Pass settings.llm and settings.state_root from weaver.config.load_config.
    Alternative adapters are tracked by #46; never silently fall back to them.
    The state_root directory must already exist (explicit config initialization).
    """
    if config.backend == "codex_cli":
        return CodexCliClient(
            config, Ledger(state_root / "llm.sqlite3"), state_root=state_root
        )
    if config.backend == "claude_cli":
        return ClaudeCliClient(
            config, Ledger(state_root / "llm.sqlite3"), state_root=state_root
        )
    if config.backend != "anthropic":
        raise UnsupportedBackend()
    return AnthropicClient(config, Ledger(state_root / "llm.sqlite3"))
