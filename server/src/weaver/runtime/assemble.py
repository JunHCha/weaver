"""Wire the resident runtime from ServerConfig; nothing here runs on import."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from weaver.alert import ScheduleAlertDetector
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
from weaver.publish import (
    AiFolder,
    EntityDirectory,
    Publisher,
    PublishState,
    ReviewRelations,
    VaultSync,
)
from weaver.schema import Source
from weaver.spinner import Spinner
from weaver.spinner.reviser import LlmReviser

from .gate import LlmGate, LlmStatus
from .ledger import RuntimeLedger
from .loop import MaintenanceTask, Runtime, VaultPublisher
from .pipeline import (
    AlertDetector,
    AlertingPublisher,
    EntityIndexSource,
    GatedLlm,
    TextSourceProcessor,
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
    alerts: AlertDetector | None = None  # #9; None means ScheduleAlertDetector
    publisher: VaultPublisher | None = None  # #7
    maintenance: Sequence[MaintenanceTask] = ()  # in addition to feedback purge
    client: LlmClient | None = None  # tests/alternative adapters


def vault_publisher(
    config: ServerConfig,
    fabric: FabricStore,
    entities: EntityDirectory,
    reviews: ReviewStore,
) -> VaultPublisher | None:
    """[publish] enabled=true wires #7; otherwise the loop keeps its no-op."""
    if not config.publish.enabled:
        return None
    state = PublishState(config.state_root / "publish.sqlite3")
    publisher = Publisher(
        AiFolder(config.vault_root, config.publish.folder),
        fabric,
        entities,
        relations=ReviewRelations(reviews),
        state=state,
        timezone=config.time.zone,
    )
    return VaultSync(publisher, fabric, entities, state)


def build_runtime(
    config: ServerConfig,
    *,
    plugins: Plugins | None = None,
    environ: Mapping[str, str] | None = None,
    interval: float = SCAN_INTERVAL_SECONDS,
) -> Runtime[Source]:
    plugins = plugins or Plugins()
    env = os.environ if environ is None else environ
    real: LlmClient | None
    if plugins.client is not None:
        real = plugins.client
        gate = LlmGate(lambda: LlmStatus(True, "injected"))
    else:
        # Constructing the client needs no credentials; the gate decides use.
        real = (
            create_client(config.llm, state_root=config.state_root)
            if config.llm.backend == "anthropic"
            else None
        )
        gate = LlmGate(lambda: llm_status(config.llm, env))
    client = GatedLlm(real, gate)
    archive = ArchiveStore.from_config(config)
    reviews = ReviewStore.from_config(config)
    store = SqliteFeedbackStore.from_config(config, reviews)
    fabric = FabricStore.from_config(config)
    publisher = CourierPublisher(reviews)
    alerting = AlertingPublisher(
        publisher,
        plugins.alerts or ScheduleAlertDetector.from_config(config, fabric),
        publisher,
    )
    handler = VerdictHandler(
        reviews,
        fabric,
        alerting,
        plugins.feedback or store,
        client,
    )
    adapter = CourierReviewAdapter(handler)
    courier = Courier(config.courier, archive, adapter, adapter)
    publisher.courier = courier
    ledger = RuntimeLedger(config.state_root / "runtime.sqlite3")
    spinner = Spinner.from_config(
        archive,
        config,
        configuration_version=CONFIGURATION_VERSION,
        reviser=LlmReviser(client),
    )
    processor = TextSourceProcessor(
        spinner=spinner,
        refiner=RuleRefiner(config.refiner),
        extractor=LlmExtractor(
            client,
            timezone=config.time.timezone,
            examples=store if config.feedback.examples else None,
        ),
        proposer=Proposer(reviews, alerting),
        reviews=reviews,
        entities=plugins.entities or store,
        cache=ledger,
        gate=gate,
    )
    return Runtime(
        state_root=config.state_root,
        scanner=courier,
        sources=archive,
        decode=Source.model_validate_json,
        processor=processor,
        ledger=ledger,
        gate=gate,
        interval=interval,
        publisher=plugins.publisher
        or vault_publisher(config, fabric, plugins.entities or store, reviews),
        maintenance=[
            MaintenanceTask(
                "feedback_purge", FEEDBACK_PURGE_INTERVAL_SECONDS, store.purge
            ),
            *plugins.maintenance,
        ],
    )
