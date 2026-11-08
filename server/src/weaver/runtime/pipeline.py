"""Source -> Spinner (text only) -> refine -> extract -> Proposer, plus defaults.

FeedbackSink and EntityIndex come from #27's SqliteFeedbackStore (wired in
``assemble``); AlertDetector (#9) defaults to a no-op. Without a usable LLM
the processor still archives, parses and refines; Sources that need the LLM
(call revision or extraction with candidates) are reported as ``waiting_llm``.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from weaver.config import LlmConfig
from weaver.llm.errors import LlmError
from weaver.loom.extract import Extractor
from weaver.loom.propose import Proposer
from weaver.loom.refine import Refiner
from weaver.loom.review_store import ReviewStore
from weaver.schema import Alert, Source, Thread
from weaver.schema.internal import EntityIndex
from weaver.spinner.pipeline import Spinner

from .loop import LlmStatus, Outcome


def llm_status(config: LlmConfig, environ: Mapping[str, str]) -> LlmStatus:
    """Decide once at startup; never reveals the key, only why it is unusable."""
    if config.backend == "none":
        return LlmStatus(False, "backend_none")
    if config.backend != "anthropic":
        return LlmStatus(False, "backend_unsupported")
    if config.api_key_env is None:
        return LlmStatus(False, "api_key_env_unset")
    if not environ.get(config.api_key_env):
        return LlmStatus(False, "api_key_missing")
    return LlmStatus(True, "configured")


class LlmUnavailable(LlmError):
    code = "llm_unavailable"


class UnavailableLlm:
    """LlmClient stand-in: approve/edit/reject still work, revise is refused."""

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        raise LlmUnavailable()

    def generate_text(self, task: str, system: str, user: str) -> str:
        raise LlmUnavailable()


class EntityIndexSource(Protocol):
    """Approved Entity registry snapshot (SqliteFeedbackStore.index, #27)."""

    def index(self) -> EntityIndex: ...


class AlertDetector(Protocol):
    """#9 boundary: alerts derived from a spun Thread."""

    def detect(self, source: Source, thread: Thread) -> list[Alert]: ...


class NoAlerts:
    def detect(self, source: Source, thread: Thread) -> list[Alert]:
        return []


class AlertSink(Protocol):
    def publish_alert(self, alert: Alert) -> Any: ...


class TextSourceProcessor:
    def __init__(
        self,
        *,
        spinner: Spinner,
        refiner: Refiner,
        extractor: Extractor | None,
        proposer: Proposer,
        reviews: ReviewStore,
        entities: EntityIndexSource,
        alerts: AlertDetector,
        alert_sink: AlertSink,
        llm_available: bool,
    ) -> None:
        self.spinner = spinner
        self.refiner = refiner
        self.extractor = extractor
        self.proposer = proposer
        self.reviews = reviews
        self.entities = entities
        self.alerts = alerts
        self.alert_sink = alert_sink
        self.llm_available = llm_available and extractor is not None

    def process(self, source: Source) -> Outcome:
        if self._republish_existing(source):
            return "done"
        if source.channel == "call" and not self.llm_available:
            return "waiting_llm"  # call scripts need the LLM Reviser
        thread = self.spinner.spin(source)
        for alert in self.alerts.detect(source, thread):
            self.alert_sink.publish_alert(alert)
        candidates = self.refiner.select(thread)
        if not candidates:
            return "done"
        if self.extractor is None or not self.llm_available:
            return "waiting_llm"
        extraction = self.extractor.extract(thread, candidates, self.entities.index())
        if extraction is not None:
            versions = getattr(self.extractor, "versions", None)
            self.proposer.build(source, extraction, versions=versions)
        return "done"

    def _republish_existing(self, source: Source) -> bool:
        """A journaled r1 (e.g. its publication failed) is republished without a
        second extraction, mirroring Proposer.build's own retry rule."""
        with self.reviews.lock():
            existing = self.reviews.source_proposal(source.source_id)
            if existing is None:
                return False
            pid = existing.proposal_id
            if self.reviews.latest_rev(pid) == 1 and not self.reviews.is_applied(
                pid, 1
            ):
                self.proposer.courier.publish_proposal(existing)
        return True
