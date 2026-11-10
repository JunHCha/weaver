"""Source -> Spinner (text only) -> refine -> extract -> Proposer, and the LLM gate.

FeedbackSink and EntityIndex come from #27's SqliteFeedbackStore (wired in
``assemble``). Change alerts (#9) run in AlertingPublisher, after every proposal
publication (r1 and revised revs), not on the raw Script. LLM availability is a
runtime gate: without a usable key the processor still archives, parses and
refines, and Sources needing the LLM (call revision, extraction with
candidates) are reported as ``waiting_llm``. The gate is re-probed while off.
"""

import logging
import threading
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from weaver.config import LlmConfig
from weaver.llm.cli import cli_status
from weaver.llm.errors import (
    AuthenticationError,
    BudgetExceeded,
    InsufficientCredit,
    UsageLimited,
)
from weaver.llm.keys import KeyUnavailable, resolve_api_key
from weaver.llm.types import LlmClient
from weaver.loom.extract import Extractor
from weaver.loom.propose import ProposalPublisher, Proposer
from weaver.loom.refine import Refiner
from weaver.loom.review_store import ReviewStore
from weaver.schema import Alert, Proposal, Source
from weaver.schema.internal import EntityIndex, Extraction
from weaver.spinner.pipeline import Spinner

from .gate import ExtractionInvalid, Interrupted, LlmGate, LlmStatus, LlmUnavailable
from .loop import Outcome

logger = logging.getLogger(__name__)


def llm_status(config: LlmConfig, environ: Mapping[str, str]) -> LlmStatus:
    """Never reveals the key or its path, only why it is unusable."""
    if config.backend == "none":
        return LlmStatus(False, "backend_none")
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


class EntityIndexSource(Protocol):
    """Approved Entity registry snapshot (SqliteFeedbackStore.index, #27)."""

    def index(self) -> EntityIndex: ...


class AlertDetector(Protocol):
    """#9 boundary: alerts for a pending proposal against approved Fabric state.

    ``published`` is called after an alert file was written, so a failed write
    is retried by the next publication instead of being remembered as sent.
    """

    def detect(self, proposal: Proposal) -> list[Alert]: ...
    def published(self, alert: Alert) -> None: ...


class NoAlerts:
    def detect(self, proposal: Proposal) -> list[Alert]:
        return []

    def published(self, alert: Alert) -> None:
        return None


class AlertSink(Protocol):
    def publish_alert(self, alert: Alert) -> Any: ...


class AlertingPublisher:
    """ProposalPublisher that publishes change alerts after each proposal.

    The proposal is published first and its failure propagates unchanged. Alerts
    are auxiliary: their failure is logged (type and proposal_id only) and never
    blocks proposal or review flow. The server never deletes alert files; the
    phone removes them (#33).
    """

    def __init__(
        self, inner: ProposalPublisher, detector: AlertDetector, sink: AlertSink
    ) -> None:
        self.inner = inner
        self.detector = detector
        self.sink = sink

    def publish_proposal(self, proposal: Proposal) -> object:
        result = self.inner.publish_proposal(proposal)
        try:
            for alert in self.detector.detect(proposal):
                self.sink.publish_alert(alert)
                self.detector.published(alert)
        except Exception as error:
            logger.warning(
                "alert publication failed: %s %s",
                type(error).__name__,
                proposal.proposal_id,
            )
        return result

    def clean_review(self, proposal_id: str, rev: int) -> None:
        self.inner.clean_review(proposal_id, rev)


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
        cache: ExtractionCache,
        gate: LlmGate,
    ) -> None:
        self.spinner = spinner
        self.refiner = refiner
        self.extractor = extractor
        self.proposer = proposer
        self.reviews = reviews
        self.entities = entities
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
