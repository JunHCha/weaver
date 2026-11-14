"""Move Actions to the phone and the phone's results back, through Courier.

``actions/<action_id>.json`` (server to phone) holds an ``Action`` the phone may
run now. ``action-results/<action_id>.json`` (phone to server) is its final
``ActionResult``. A result becomes one ``실행`` Thread in Fabric and then
updates the outbox; replaying the same file is a no-op, so a crash between the
two steps only repeats idempotent work.
"""

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from weaver.courier.atomic import write_bytes_atomic
from weaver.courier.files import (
    CourierFileError,
    is_conflict,
    is_ignored,
    load_versioned,
)
from weaver.fabric import Fabric, FabricError
from weaver.schema import Action, ActionResult, ExecutionValue, ThreadDraft

from .outbox import ActionOutbox, ActionRecord, execution_thread_id

logger = logging.getLogger("weaver.action")

ACTIONS = "actions"
RESULTS = "action-results"
LEGACY_SCHEDULES = ("proposals", "schedules")
_NAME = re.compile(r"^(act_[0-9a-f]{32})\.json$")
# A crash leftover of ``write_bytes_atomic`` for an Action file.
_TEMPORARY = re.compile(r"^\.act_[0-9a-f]{32}\.json\.[0-9a-f]{16}\.tmp$")
STALE_TEMPORARY_SECONDS = 600.0
RETRY_SECONDS = 60.0
MAX_RETRY_SECONDS = 3600.0


class ActionCourier:
    """Publish dispatchable Actions and record the results the phone reports."""

    def __init__(
        self,
        root: Path,
        outbox: ActionOutbox,
        fabric: Fabric,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.outbox = outbox
        self.fabric = fabric
        self.clock = clock

    @property
    def actions(self) -> Path:
        return self.root / ACTIONS

    @property
    def results(self) -> Path:
        return self.root / RESULTS

    def legacy_present(self) -> bool:
        directory = self.root.joinpath(*LEGACY_SCHEDULES)
        return directory.is_dir() and any(directory.glob("*.json"))

    def retire_legacy(self) -> None:
        """Remove the old ``proposals/schedules`` projection.

        The Action path replaces it. An old phone app must not offer a manual
        registration for a schedule the new path also registers automatically.
        """
        directory = self.root.joinpath(*LEGACY_SCHEDULES)
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix == ".json":
                path.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass  # dotfile temporaries or a sync conflict copy remain

    # ---- server to phone -------------------------------------------------------------

    def publish(self) -> list[str]:
        """Write offered Actions and remove the files of finished ones."""
        self.actions.mkdir(parents=True, exist_ok=True)
        offered = self.outbox.dispatchable(self.clock())
        wanted: dict[str, bytes] = {}
        for record, external_id in offered:
            action = to_wire(record, external_id)
            wanted[f"{record.action_id}.json"] = action.model_dump_json().encode()
        written = []
        for name, data in wanted.items():
            target = self.actions / name
            if target.is_file() and target.read_bytes() == data:
                continue
            write_bytes_atomic(target, data)
            written.append(name)
        for path in self.actions.iterdir():
            if _TEMPORARY.fullmatch(path.name):
                if path.lstat().st_mtime < self.clock() - STALE_TEMPORARY_SECONDS:
                    path.unlink(missing_ok=True)
                continue
            match = _NAME.fullmatch(path.name)
            if match is None or path.name in wanted:
                continue
            known = self.outbox.get(match.group(1))
            if known is not None and known.status != "pending":
                path.unlink(missing_ok=True)
        return written

    # ---- phone to server -------------------------------------------------------------

    def ingest(self) -> dict[str, str]:
        """Apply every reported result; return a problem code per unusable file."""
        problems: dict[str, str] = {}
        if not self.results.is_dir():
            return problems
        for path in sorted(self.results.iterdir()):
            name = path.name
            if is_ignored(name):
                continue
            if is_conflict(name):
                problems[name] = "sync_conflict"
                continue
            match = _NAME.fullmatch(name)
            if match is None:
                continue
            try:
                result = load_versioned(path, ActionResult, version=2)
                if result.action_id != match.group(1):
                    raise CourierFileError("id_mismatch")
                self._apply(result)
            except CourierFileError as error:
                problems[name] = error.code
                logger.warning("action result %s: %s", name, error.code)
                continue
            except FabricError as error:
                problems[name] = "fabric_failed"
                logger.warning("action result %s: %s", name, type(error).__name__)
                self._backoff(result.action_id)
                continue
            path.unlink(missing_ok=True)
        return problems

    def _apply(self, result: ActionResult) -> None:
        record = self.outbox.get(result.action_id)
        if record is None:
            raise CourierFileError("unknown_action")
        if record.status != "pending":
            if (record.status, record.external_id, record.error) == (
                result.outcome,
                result.external_id,
                result.error,
            ):
                return  # the same report again
            raise CourierFileError("result_conflict")
        if record.next_attempt_at is not None and record.next_attempt_at > self.clock():
            raise CourierFileError("apply_backoff")
        self.fabric.append([execution_draft(record, result)])
        self.outbox.finish(
            result.action_id,
            outcome=result.outcome,
            external_id=result.external_id,
            error=result.error,
            attempts=result.attempts,
        )

    def _backoff(self, action_id: str) -> None:
        record = self.outbox.get(action_id)
        if record is None:
            return
        delay = min(RETRY_SECONDS * 2**record.attempts, MAX_RETRY_SECONDS)
        self.outbox.record_error(action_id, "fabric_failed", self.clock() + delay)


def to_wire(record: ActionRecord, external_id: str | None) -> Action:
    return Action(
        schema_version=2,
        action_id=record.action_id,
        kind=record.kind,
        thread_id=record.thread_id,
        seq=record.seq,
        input_version=record.input_version,
        payload=record.payload,
        external_id=external_id,
    )


def execution_draft(record: ActionRecord, result: ActionResult) -> ThreadDraft:
    """The ``실행`` Thread for a result.

    Times come from the report, not the clock, so a replay is byte-identical
    and Fabric accepts it as the same row.
    """
    value = ExecutionValue(
        action_id=record.action_id,
        action=record.kind,
        outcome=result.outcome,
        external_id=result.external_id,
        error=result.error,
    )
    return ThreadDraft(
        thread_id=execution_thread_id(record.action_id),
        kind="실행",
        content=f"{record.kind} {result.outcome}",
        occurred_at=result.finished_at,
        recorded_at=result.finished_at,
        tags=[],
        field=None,
        value=value.model_dump(mode="json"),
        source_id=record.source_id,
        actor="system",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
        executes=record.thread_id,
    )
