"""LLM availability for the runtime: the status probe and the gated client.

Without a usable key the runtime still archives and parses Sources; work that
needs the LLM (call revision, drafting, approval) is reported as
``waiting_llm`` and retried when the gate is re-probed.
"""

from collections.abc import Mapping

from pydantic import BaseModel

from weaver.config import LlmConfig
from weaver.llm.cli import cli_status
from weaver.llm.codex import codex_status
from weaver.llm.errors import (
    AuthenticationError,
    BudgetExceeded,
    InsufficientCredit,
    UsageLimited,
)
from weaver.llm.keys import KeyUnavailable, resolve_api_key
from weaver.llm.types import LlmClient

from .gate import LlmGate, LlmStatus, LlmUnavailable


def llm_status(config: LlmConfig, environ: Mapping[str, str]) -> LlmStatus:
    """Never reveals the key or its path, only why it is unusable."""
    if config.backend == "none":
        return LlmStatus(False, "backend_none")
    if config.backend == "codex_cli":
        status = codex_status(config, environ)
        return LlmStatus(status.available, status.reason)
    if config.backend == "claude_cli":
        status = cli_status(config, environ)
        return LlmStatus(status.available, status.reason)
    if config.backend != "anthropic":
        return LlmStatus(False, "backend_unsupported")
    try:
        resolve_api_key(config, environ)
    except KeyUnavailable as error:
        return LlmStatus(False, error.reason)
    return LlmStatus(True, "configured")


class GatedLlm:
    """LlmClient that refuses calls while the gate is off (or no backend).

    A refused call raises LlmUnavailable; the work stays queued for later.
    """

    def __init__(self, client: LlmClient | None, gate: LlmGate) -> None:
        self.client = client
        self.gate = gate
        self.model = getattr(client, "model", "unknown")

    def _target(self) -> LlmClient:
        if self.client is None or not self.gate.available:
            raise LlmUnavailable()
        return self.client

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        try:
            return self._target().generate_json(task, system, user, schema)
        except (
            AuthenticationError,
            BudgetExceeded,
            InsufficientCredit,
            UsageLimited,
        ) as error:
            self.gate.disable(error.code)
            raise

    def generate_text(self, task: str, system: str, user: str) -> str:
        try:
            return self._target().generate_text(task, system, user)
        except (
            AuthenticationError,
            BudgetExceeded,
            InsufficientCredit,
            UsageLimited,
        ) as error:
            self.gate.disable(error.code)
            raise
