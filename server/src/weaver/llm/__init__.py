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
from .ledger import Ledger, MemoryLedger
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


def create_client(
    config: LlmConfig, *, state_root: Path, dry_run: bool = False
) -> LlmClient:
    """Construct without credentials/network; resolve api_key_env per call.

    Pass settings.llm and settings.state_root from weaver.config.load_config.
    Alternative adapters are tracked by #46; never silently fall back to them.
    The state_root directory must already exist (explicit config initialization).
    """
    if config.backend not in ("anthropic", "claude_cli", "codex_cli"):
        raise UnsupportedBackend()
    ledger = (
        MemoryLedger(state_root / "llm.sqlite3")
        if dry_run
        else Ledger(state_root / "llm.sqlite3")
    )
    if config.backend in ("codex_cli", "claude_cli"):
        client: ClaudeCliClient = (
            CodexCliClient(config, ledger, state_root=state_root)
            if config.backend == "codex_cli"
            else ClaudeCliClient(config, ledger, state_root=state_root)
        )
        if dry_run:
            # The CLI's own temporary protocol files are intrinsic to that
            # backend, but must not create a Weaver state/cache directory.
            import tempfile

            client.workdir = Path(tempfile.gettempdir())
        return client
    if config.backend != "anthropic":
        raise UnsupportedBackend()
    return AnthropicClient(config, ledger)
