"""Wire the resident runtime from ServerConfig; nothing here runs on import."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from weaver.action import ActionCourier, ActionJob, ActionOutbox
from weaver.alert import ScheduleAlertDetector
from weaver.alert.delivery import AlertDelivery
from weaver.archive import ArchiveStore
from weaver.config import ServerConfig
from weaver.context import ContextDictionary
from weaver.context.proposals import LocationProposals
from weaver.courier import SCAN_INTERVAL_SECONDS, Courier
from weaver.fabric import FabricStore
from weaver.ingest import VerdictIngest
from weaver.llm import create_client
from weaver.llm.cli import ClaudeCliClient
from weaver.llm.ledger import Ledger
from weaver.llm.policy import LedgerBacklog, LlmPolicy, PolicyClient
from weaver.llm.policy_archive import configured_policy
from weaver.llm.types import LlmClient
from weaver.loom.summary import SummaryDrafts
from weaver.loom.summary_review import SummaryReview
from weaver.schema import Source
from weaver.spinner import Spinner
from weaver.spinner.reviser import LlmReviser
from weaver.spinner.webex import WebexProvenanceStore
from weaver.wiki.home import check_vault_ignored
from weaver.wiki.store import WikiStore

from .gate import LlmGate, LlmStatus
from .ledger import RuntimeLedger
from .loop import MaintenanceTask, Runtime
from .pipeline import GatedLlm, llm_status
from .progress import PipelineJournal, PipelinePublisher
from .summary import (
    ObservationPublisher,
    ReviewAdapter,
    SummaryLookup,
    SummaryProcessor,
)
from .wiki import WikiJob

# Part of Spinner's cache key: bump when runtime wiring changes Script output.
CONFIGURATION_VERSION = "weaver.runtime.v3"


@dataclass
class Plugins:
    """Overrides for tests and alternative adapters."""

    maintenance: Sequence[MaintenanceTask] = ()
    client: LlmClient | None = None
    # Tests may replace the archive-backed gate; an injected client is still gated.
    policy: LlmPolicy | None = None


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
    policy = plugins.policy or configured_policy(config)
    backlog = policy.backlog or LedgerBacklog(Ledger(config.state_root / "llm.sqlite3"))
    policy.backlog = backlog
    if plugins.client is not None:
        # An injected client never bypasses the central gate.
        real = PolicyClient(plugins.client, policy)
        gate = LlmGate(lambda: LlmStatus(True, "injected"))
    else:
        # Constructing the client needs no credentials; the gate decides use.
        real = (
            create_client(
                config.llm,
                state_root=config.state_root,
                policy=policy,
            )
            if config.llm.backend in {"anthropic", "claude_cli", "codex_cli"}
            else None
        )
        gate = LlmGate(lambda: llm_status(config.llm, env))
    client = GatedLlm(real, gate)
    archive = ArchiveStore.from_config(config)
    context = ContextDictionary(config.state_root, archive_root=config.archive_root)
    fabric = FabricStore.from_config(config)
    webex = WebexProvenanceStore(config.state_root)
    summaries = SummaryLookup(config.archive_root / "summaries", archive, webex)
    location_proposals = LocationProposals(config.state_root)
    review = SummaryReview(
        client,
        context,
        fabric,
        summaries.archive,
        state_root=config.state_root,
        proposals=config.courier.proposals,
        publish_files=config.mobile.transport_for("review") == "files",
        timezone=config.time.timezone,
        default_sphere=config.reference.default_sphere,
        location_proposals=location_proposals,
    )
    progress = PipelinePublisher(
        PipelineJournal(
            config.state_root,
            config.courier.proposals,
            failure_limit=config.courier_settings.max_attempts,
            publish_files=config.mobile.publishes_pipeline_files(),
        ),
        archive,
        review.state,
        fabric,
        config.courier.proposals,
        context=context,
    )
    adapter = ReviewAdapter(review, summaries, progress)
    ingest = VerdictIngest(
        adapter,
        adapter,
        retry=config.courier_settings,
        can_apply=lambda: gate.available,
        on_apply_stopped=progress.stopped,
    )
    courier = Courier(config.courier, archive, adapter, adapter, ingest=ingest)
    ledger = RuntimeLedger(config.state_root / "runtime.sqlite3")
    drafts = SummaryDrafts(
        client,
        context,
        config.courier.proposals,
        state_root=config.state_root,
        publish_files=config.mobile.transport_for("review") == "files",
    )
    spinner = Spinner.from_config(
        archive,
        config,
        configuration_version=CONFIGURATION_VERSION,
        reviser=LlmReviser(client),
        revision_threshold=drafts.revision_threshold,
    )
    processor = SummaryProcessor(
        spinner, drafts, gate, progress, policy=policy, webex=webex
    )

    maintenance = list(plugins.maintenance)
    maintenance.append(MaintenanceTask("review-projection", interval, review.republish))
    if config.wiki.enabled:
        # Before WikiStore, whose constructor creates the folder.
        check_vault_ignored(
            config.wiki.vault_root or config.vault_root, config.wiki.folder
        )
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
            journal=progress.journal,
            default_sphere=config.reference.default_sphere,
            location_proposals=location_proposals,
        )
        maintenance.append(
            MaintenanceTask(
                "wiki",
                config.wiki.interval_seconds,
                job.run,
                allowed=real.wiki_available
                if isinstance(real, ClaudeCliClient)
                else None,
            )
        )

    # Calendar commands: derived from Fabric, run by the phone, results come
    # back as 실행 Threads. Not LLM-gated; it only reads and writes local files.
    outbox = ActionOutbox(config.state_root / "action-outbox.sqlite3")
    action_courier = ActionCourier(
        config.sync_root,
        outbox,
        fabric,
        # While HTTP owns dispatch, no Action file is offered to a second executor.
        publish_files=lambda: config.mobile.transport_for("action") == "files",
    )
    actions = ActionJob(fabric, outbox, action_courier)
    maintenance.append(MaintenanceTask("actions", interval, actions.run))

    if config.mobile.bind is not None:
        # Phone HTTP API operations are journaled by `weaver mobile serve` and
        # finished here, through the same decision/action journals as files.
        from weaver.mobile.applier import MobileApplier
        from weaver.mobile.reviews import ReviewService
        from weaver.mobile.store import MobileStore
        from weaver.mobile.transport import MobileRuntime

        mobile_store = MobileStore(config.state_root)
        mobile = MobileRuntime(
            config,
            mobile_store,
            MobileApplier(mobile_store, ingest, action_courier),
            ReviewService(review.state, mobile_store),
            action_courier,
        )
        maintenance.append(MaintenanceTask("mobile", interval, mobile.run))

    return Runtime(
        state_root=config.state_root,
        scanner=courier,
        sources=archive,
        decode=Source.model_validate_json,
        processor=processor,
        ledger=ledger,
        gate=gate,
        interval=interval,
        policy_held=backlog.pending,
        publisher=ObservationPublisher(
            fabric,
            review.state,
            (ScheduleAlertDetector.from_config(config), AlertDelivery(config)),
            progress,
        ),
        maintenance=maintenance,
    )
