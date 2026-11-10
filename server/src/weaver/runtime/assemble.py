"""Wire the resident runtime from ServerConfig; nothing here runs on import."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from weaver.archive import ArchiveStore
from weaver.config import ServerConfig
from weaver.context import ContextDictionary
from weaver.courier import SCAN_INTERVAL_SECONDS
from weaver.courier.courier_v2 import CourierV2
from weaver.fabric.store import FabricStore
from weaver.fabric.v2 import FabricStore as FabricV2
from weaver.llm import create_client
from weaver.llm.types import LlmClient
from weaver.loom import (
    FeedbackSink,
    ReviewStore,
)
from weaver.loom.summary import SummaryDrafts
from weaver.loom.summary_review import SummaryReview
from weaver.publish import (
    AiFolder,
    EntityDirectory,
    OutputSync,
    Publisher,
    PublishState,
    ReviewRelations,
    SqliteOutputWatcher,
    VaultSync,
)
from weaver.schema import Source
from weaver.spinner import Spinner
from weaver.spinner.reviser import LlmReviser
from weaver.wiki.store import WikiStore

from .gate import LlmGate, LlmStatus
from .ledger import RuntimeLedger
from .loop import MaintenanceTask, Runtime, VaultPublisher
from .pipeline import (
    AlertDetector,
    EntityIndexSource,
    GatedLlm,
    llm_status,
)
from .summary import ReviewAdapter, SchedulePublisher, SummaryLookup, SummaryProcessor
from .wiki import WikiJob

# Part of Spinner's cache key: bump when runtime wiring changes Script output.
CONFIGURATION_VERSION = "weaver.runtime.v2"
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
    watcher = SqliteOutputWatcher(
        publisher.folder,
        config.state_root / "output-watcher.sqlite3",
        timezone=config.time.zone,
        manifest=state,
    )
    return VaultSync(
        publisher, fabric, entities, state, outputs=OutputSync(watcher, fabric)
    )


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
            if config.llm.backend in {"anthropic", "claude_cli"}
            else None
        )
        gate = LlmGate(lambda: llm_status(config.llm, env))
    client = GatedLlm(real, gate)
    archive = ArchiveStore.from_config(config)
    context = ContextDictionary(config.state_root)
    fabric_v2 = FabricV2.from_config(config)
    summaries = SummaryLookup(config.archive_root / "summaries", archive)
    review = SummaryReview(
        client,
        context,
        fabric_v2,
        summaries.archive,
        state_root=config.state_root,
        proposals=config.courier.proposals,
        timezone=config.time.timezone,
    )
    adapter = ReviewAdapter(review, summaries)
    courier = CourierV2(config.courier, archive, adapter, adapter)
    ledger = RuntimeLedger(config.state_root / "runtime.sqlite3")
    spinner = Spinner.from_config(
        archive,
        config,
        configuration_version=CONFIGURATION_VERSION,
        reviser=LlmReviser(client),
    )
    drafts = SummaryDrafts(
        client, context, config.courier.proposals, state_root=config.state_root
    )
    processor = SummaryProcessor(spinner, drafts, gate)

    maintenance = list(plugins.maintenance)
    if config.wiki.enabled:
        store = WikiStore(
            config.wiki.vault_root or config.vault_root,
            config.state_root,
            wiki_root=config.wiki.folder,
            settle_seconds=config.wiki.settle_seconds,
        )
        job = WikiJob(
            store,
            fabric_v2,
            context,
            client,
            config.state_root,
            ZoneInfo(config.wiki.timezone),
            summaries,
        )
        maintenance.append(
            MaintenanceTask("wiki", config.wiki.interval_seconds, job.run)
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
        publisher=SchedulePublisher(
            config.sync_root, fabric_v2, review.state, plugins.publisher
        ),
        maintenance=maintenance,
    )
