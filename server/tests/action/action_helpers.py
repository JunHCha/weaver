"""Synthetic Threads and results for Action tests; no LLM, phone or vault."""

from datetime import UTC, datetime
from pathlib import Path

from weaver.action import ActionCourier, ActionEngine, ActionJob, ActionOutbox
from weaver.action.outbox import ActionRecord
from weaver.fabric import FabricStore
from weaver.schema import ActionResult, ThreadDraft

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
WHEN = datetime(2026, 11, 7, tzinfo=UTC)


def draft(key: str, **changes: object) -> ThreadDraft:
    data: dict[str, object] = dict(
        thread_id=key,
        kind="기록",
        content=key,
        occurred_at=WHEN,
        recorded_at=WHEN,
        tags=[],
        field=None,
        value=None,
        source_id=SOURCE,
        actor="ai_approved",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )
    data.update(changes)
    return ThreadDraft.model_validate(data)


def schedule(
    key: str,
    *,
    title: str = "주간 회의",
    start: str = "2026-12-01T10:00:00+09:00",
    tags: tuple[str, ...] = ("홍길동",),
    blocked_by: str | None = None,
    updates: str | None = None,
) -> ThreadDraft:
    return draft(
        key,
        kind="일정",
        content=title,
        tags=list(tags),
        field="schedule",
        value={"start": start, "end": None, "timezone": "Asia/Seoul"},
        blocked_by=blocked_by,
        updates=updates,
    )


def request(key: str) -> ThreadDraft:
    return draft(key, kind="요청", direction="받음")


def decision(key: str, resolves: str) -> ThreadDraft:
    return draft(key, kind="결정", resolves=resolves)


def retraction(key: str, target: str) -> ThreadDraft:
    return draft(key, retracts=target)


class Rig:
    """A Fabric, an outbox and a Courier root in one temporary directory."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.root = tmp_path / "sync"
        self.root.mkdir(exist_ok=True)
        self.fabric = FabricStore(tmp_path / "fabric.sqlite3")
        self.open()

    def open(self) -> None:
        """Reopen every store, as a restart does."""
        self.outbox = ActionOutbox(self.tmp_path / "action-outbox.sqlite3")
        self.engine = ActionEngine(self.fabric, self.outbox)
        self.courier = ActionCourier(self.root, self.outbox, self.fabric)
        self.job = ActionJob(self.fabric, self.outbox, self.courier)

    def add(self, *drafts: ThreadDraft) -> None:
        self.fabric.append(list(drafts))

    def actions(self) -> list[ActionRecord]:
        return self.outbox.all()

    def kinds(self) -> list[str]:
        return [a.kind for a in self.actions()]

    def report(
        self,
        record: ActionRecord,
        outcome: str = "completed",
        *,
        external_id: str | None = "content://calendar/events/7",
        error: str | None = None,
        finished: str = "2026-11-07T15:45:00+09:00",
    ) -> ActionResult:
        """Write the result file the phone would sync back."""
        if outcome == "failed":
            external_id, error = None, error or "provider_error"
        result = ActionResult.model_validate(
            {
                "schema_version": 2,
                "action_id": record.action_id,
                "outcome": outcome,
                "external_id": external_id,
                "attempts": 1,
                "finished_at": finished,
                "error": error,
            }
        )
        self.courier.results.mkdir(parents=True, exist_ok=True)
        (self.courier.results / f"{record.action_id}.json").write_text(
            result.model_dump_json()
        )
        return result
