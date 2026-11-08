"""Source -> Spinner (text only) -> refine -> extract -> Proposer, and the LLM gate.

FeedbackSink and EntityIndex come from #27's SqliteFeedbackStore (wired in
``assemble``); AlertDetector (#9) defaults to a no-op. LLM availability is a
runtime gate: without a usable key the processor still archives, parses and
refines, and Sources needing the LLM (call revision, extraction with
candidates) are reported as ``waiting_llm``. The gate is re-probed while off.
"""

import threading
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from weaver.config import LlmConfig
from weaver.llm.keys import KeyUnavailable, resolve_api_key
from weaver.llm.types import LlmClient
from weaver.loom.extract import Extractor
from weaver.loom.propose import Proposer
from weaver.loom.refine import Refiner
from weaver.loom.review_store import ReviewStore
from weaver.schema import Alert, Source, Thread
from weaver.schema.internal import EntityIndex, Extraction
from weaver.spinner.pipeline import Spinner

from .gate import ExtractionInvalid, Interrupted, LlmGate, LlmStatus, LlmUnavailable
from .loop import Outcome


def llm_status(config: LlmConfig, environ: Mapping[str, str]) -> LlmStatus:
    """Never reveals the key or its path, only why it is unusable."""
    if config.backend == "none":
        return LlmStatus(False, "backend_none")
    if config.backend != "anthropic":
        return LlmStatus(False, "backend_unsupported")
    try:
        resolve_api_key(config, environ)
    except KeyUnavailable as error:
        return LlmStatus(False, error.reason)
    return LlmStatus(True, "configured")


class GatedLlm:
    """LlmClient that refuses calls while the gate is off (or no backend).

    VerdictHandler gets this: approve/edit/reject never call the LLM and keep
    working; revise raises LlmUnavailable and its decision stays for later.
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
        return self._target().generate_json(task, system, user, schema)

    def generate_text(self, task: str, system: str, user: str) -> str:
        return self._target().generate_text(task, system, user)


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


class ExtractionCache(Protocol):
    def cached_extraction(
        self, source_id: str, versions: Mapping[str, str] | None
    ) -> str | None: ...

    def cache_extraction(
        self, source_id: str, versions: Mapping[str, str] | None, extraction: str
    ) -> None: ...


def _check(stop: threading.Event) -> None:
    if stop.is_set():
        raise Interrupted()


class TextSourceProcessor:
    def __init__(
        self,
        *,
        spinner: Spinner,
        refiner: Refiner,
        extractor: Extractor,
        proposer: Proposer,
        reviews: ReviewStore,
        entities: EntityIndexSource,
        alerts: AlertDetector,
        alert_sink: AlertSink,
        cache: ExtractionCache,
        gate: LlmGate,
    ) -> None:
        self.spinner = spinner
        self.refiner = refiner
        self.extractor = extractor
        self.proposer = proposer
        self.reviews = reviews
        self.entities = entities
        self.alerts = alerts
        self.alert_sink = alert_sink
        self.cache = cache
        self.gate = gate

    def process(self, source: Source, stop: threading.Event) -> Outcome:
        """Checks ``stop`` before every stage that may call the LLM or journal."""
        if self._republish_existing(source):
            return "done"
        if source.channel == "call" and not self.gate.available:
            return "waiting_llm"  # call scripts need the LLM Reviser
        _check(stop)
        thread = self.spinner.spin(source)
        for alert in self.alerts.detect(source, thread):
            self.alert_sink.publish_alert(alert)
        candidates = self.refiner.select(thread)
        if not candidates:
            return "done"
        versions: dict[str, str] | None = getattr(self.extractor, "versions", None)
        cached = self.cache.cached_extraction(source.source_id, versions)
        extraction: Extraction | None
        if cached is not None:
            extraction = (
                None if cached == "null" else Extraction.model_validate_json(cached)
            )
        else:
            if not self.gate.available:
                return "waiting_llm"
            _check(stop)
            try:
                extraction = self.extractor.extract(
                    thread, candidates, self.entities.index()
                )
            except ValueError as error:
                raise ExtractionInvalid(type(error).__name__) from None
            # Persist the paid result before anything else can fail.
            self.cache.cache_extraction(
                source.source_id,
                versions,
                "null" if extraction is None else extraction.model_dump_json(),
            )
        if extraction is None:
            return "done"
        _check(stop)
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
