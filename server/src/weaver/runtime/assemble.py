"""Wire the resident runtime from ServerConfig; nothing here runs on import."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from weaver.archive import ArchiveStore
from weaver.config import ServerConfig
from weaver.courier import SCAN_INTERVAL_SECONDS, Courier
from weaver.fabric.store import FabricStore
from weaver.llm import create_client
from weaver.llm.types import LlmClient
from weaver.loom import (
    CourierPublisher,
    CourierReviewAdapter,
    FeedbackSink,
    LlmExtractor,
    Proposer,
    ReviewStore,
    RuleRefiner,
    SqliteFeedbackStore,
    VerdictHandler,
)
from weaver.schema import Source
from weaver.spinner import Spinner
from weaver.spinner.reviser import LlmReviser

from .ledger import RuntimeLedger
from .loop import LlmStatus, MaintenanceTask, Runtime, VaultPublisher
from .pipeline import (
    AlertDetector,
    EntityIndexSource,
    NoAlerts,
    TextSourceProcessor,
    UnavailableLlm,
    llm_status,
)

# Part of Spinner's cache key: bump when runtime wiring changes Thread output.
CONFIGURATION_VERSION = "weaver.runtime.v1"
# Raw-fragment retention is counted in days; a few runs a day are plenty.
FEEDBACK_PURGE_INTERVAL_SECONDS = 6 * 3600.0


@dataclass
class Plugins:
    """Overrides; None means the #27 SqliteFeedbackStore (sink, index, purge)."""

    feedback: FeedbackSink | None = None
    entities: EntityIndexSource | None = None
    alerts: AlertDetector = field(default_factory=NoAlerts)  # #9
    publisher: VaultPublisher | None = None  # #7
    maintenance: Sequence[MaintenanceTask] = ()  # in addition to feedback purge
    client: LlmClient | None = None  # tests/alternative adapters


def build_runtime(
    config: ServerConfig,
    *,
    plugins: Plugins | None = None,
    environ: Mapping[str, str] | None = None,
    interval: float = SCAN_INTERVAL_SECONDS,
) -> Runtime[Source]:
    plugins = plugins or Plugins()
    env = os.environ if environ is None else environ
    llm = llm_status(config.llm, env)
    client: LlmClient
    if plugins.client is not None:
        client = plugins.client
        llm = LlmStatus(True, "injected")
    elif llm.available:
        client = create_client(config.llm, state_root=config.state_root)
    else:
        client = UnavailableLlm()
    archive = ArchiveStore.from_config(config)
    reviews = ReviewStore.from_config(config)
    store = SqliteFeedbackStore.from_config(config, reviews)
    publisher = CourierPublisher(reviews)
    handler = VerdictHandler(
        reviews,
        FabricStore.from_config(config),
        publisher,
        plugins.feedback or store,
        client,
    )
    adapter = CourierReviewAdapter(handler)
    courier = Courier(config.courier, archive, adapter, adapter)
    publisher.courier = courier
    usable = llm.available
    spinner = Spinner.from_config(
        archive,
        config,
        configuration_version=CONFIGURATION_VERSION,
        reviser=LlmReviser(client) if usable else None,
    )
    processor = TextSourceProcessor(
        spinner=spinner,
        refiner=RuleRefiner(config.refiner),
        extractor=LlmExtractor(
            client,
            timezone=config.time.timezone,
            examples=store if config.feedback.examples else None,
        )
        if usable
        else None,
        proposer=Proposer(reviews, publisher),
        reviews=reviews,
        entities=plugins.entities or store,
        alerts=plugins.alerts,
        alert_sink=courier,
        llm_available=usable,
    )
    return Runtime(
        state_root=config.state_root,
        scanner=courier,
        sources=archive,
        processor=processor,
        ledger=RuntimeLedger(config.state_root / "runtime.sqlite3"),
        llm=llm,
        interval=interval,
        publisher=plugins.publisher,
        maintenance=[
            MaintenanceTask(
                "feedback_purge", FEEDBACK_PURGE_INTERVAL_SECONDS, store.purge
            ),
            *plugins.maintenance,
        ],
    )
