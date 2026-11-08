"""LLM availability gate and the runtime's error markers."""

from collections.abc import Callable
from dataclasses import dataclass

from weaver.llm.errors import LlmError


@dataclass(frozen=True)
class LlmStatus:
    available: bool
    reason: str


class Interrupted(Exception):
    """Stop was requested before the next LLM/journal stage; not a failure."""


class LlmGate:
    """Current LLM availability; disabled on auth/budget errors, re-probed."""

    def __init__(self, probe: Callable[[], LlmStatus]) -> None:
        self._probe = probe
        self.status = probe()

    @property
    def available(self) -> bool:
        return self.status.available

    def disable(self, reason: str) -> None:
        self.status = LlmStatus(False, reason)

    def refresh(self) -> LlmStatus:
        self.status = self._probe()
        return self.status


class LlmUnavailable(LlmError):
    code = "llm_unavailable"


class ExtractionInvalid(Exception):
    """LLM extraction output failed validation; a contract violation (not
    ValueError, so it gets the bounded contract retry rule)."""
