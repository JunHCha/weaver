"""Wire the resident runtime from ServerConfig; nothing here runs on import."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from weaver.alert import ScheduleAlertDetector
from weaver.archive import ArchiveStore
from weaver.config import ServerConfig
from weaver.context import ContextDictionary
from weaver.courier import SCAN_INTERVAL_SECONDS, Courier
from weaver.fabric import FabricStore
from weaver.llm import create_client
from weaver.llm.types import LlmClient
from weaver.loom.summary import SummaryDrafts
from weaver.loom.summary_review import SummaryReview
from weaver.schema import Source
from weaver.spinner import Spinner
from weaver.spinner.reviser import LlmReviser
from weaver.wiki.store import WikiStore

from .gate import LlmGate, LlmStatus
from .ledger import RuntimeLedger
from .loop import MaintenanceTask, Runtime
from .pipeline import GatedLlm, llm_status
from .progress import PipelineJournal, PipelinePublisher
from .summary import ReviewAdapter, SchedulePublisher, SummaryLookup, SummaryProcessor
from .wiki import WikiJob

# Part of Spinner's cache key: bump when runtime wiring changes Script output.
CONFIGURATION_VERSION = "weaver.runtime.v2"


@dataclass
class Plugins:
    """Overrides for tests and alternative adapters."""

    maintenance: Sequence[MaintenanceTask] = ()
    client: LlmClient | None = None


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
    fabric = FabricStore.from_config(config)
    summaries = SummaryLookup(config.archive_root / "summaries", archive)
    review = SummaryReview(
        client,
        context,
        fabric,
        summaries.archive,
        state_root=config.state_root,
        proposals=config.courier.proposals,
        timezone=config.time.timezone,
    )
    progress = PipelinePublisher(
        PipelineJournal(
            config.state_root,
            config.courier.proposals,
            failure_limit=config.courier_settings.max_attempts,
        ),
        archive,
        review.state,
        fabric,
        config.courier.proposals,
    )
    adapter = ReviewAdapter(review, summaries, progress)
    courier = Courier(
        config.courier,
        archive,
        adapter,
        adapter,
        retry=config.courier_settings,
        can_apply=lambda: gate.available,
        on_apply_stopped=progress.stopped,
    )
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
    processor = SummaryProcessor(spinner, drafts, gate, progress)

    maintenance = list(plugins.maintenance)
    if config.wiki.enabled:
        store = WikiStore(
            config.wiki.vault_root or config.vault_root,
            config.state_root,
            wiki_root=config.wiki.folder,
            settle_seconds=config.wiki.settle_seconds,
        )
        progress.wiki = store
        progress.vault = config.wiki.vault_root or config.vault_root
        job = WikiJob(
            store,
            fabric,
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
            config.sync_root,
            fabric,
            review.state,
            (ScheduleAlertDetector.from_config(config), courier),
            progress,
        ),
        maintenance=maintenance,
    )
