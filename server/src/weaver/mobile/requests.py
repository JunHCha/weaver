"""Human request decisions, using the mobile edit journal and Fabric write lock."""

import hashlib
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from weaver.fabric import FabricStore
from weaver.schema import Thread, ThreadDraft
from weaver.web.editing import EditRejected, canonical_threads

from .edits import _LOCK, ClientId, EditService
from .read import decorate
from .store import Operation

Text = Annotated[str, Field(strict=True, min_length=1, max_length=2000, pattern=r"\S")]


class RequestEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: ClientId
    thread_id: Text
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    content: Text


class DeadlineEdit(RequestEdit):
    deadline: Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]


class UndoEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: ClientId
    edit_thread_id: Text
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


def source_id(operation: Operation) -> str:
    number = int(hashlib.sha256(operation.operation_id.encode()).hexdigest()[:32], 16)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "src_" + "".join(
        alphabet[(number >> (5 * i)) & 31] for i in reversed(range(26))
    )


class RequestEditService(EditService):
    def apply(
        self,
        device_id: str,
        name: Literal["complete", "deadline", "undo"],
        edit: RequestEdit | UndoEdit,
    ) -> tuple[Operation, bool]:
        with _LOCK:
            operation, _ = self._journal(
                device_id, "requests", name, edit, edit.client_id
            )
            new_id = f"phone-request:{operation.operation_id}"
            stamp = datetime.fromisoformat(operation.created_at)
            result: dict[str, object] = {}

            def result_for(row: Thread) -> dict[str, object]:
                target_id = row.resolves or row.amends
                if row.retracts:
                    original = next(
                        t for t in current_rows if t.thread_id == row.retracts
                    )
                    target_id = original.resolves or original.amends
                return {
                    "edit_thread_id": row.thread_id,
                    "thread_id": target_id,
                    "revision": row.seq,
                    "changed": True,
                }

            current_rows: list[Thread] = []

            def build(raw: list[Thread]) -> list[ThreadDraft]:
                nonlocal result, current_rows
                current_rows = raw
                # Recover an append whose operation-result commit was interrupted.
                prior = next((t for t in raw if t.thread_id == new_id), None)
                if prior is not None:
                    result = result_for(prior)
                    return []
                original: Thread | None = None
                if isinstance(edit, UndoEdit):
                    original = next(
                        (t for t in raw if t.thread_id == edit.edit_thread_id), None
                    )
                    if original is None:
                        raise EditRejected(404, "thread_not_found", "thread_not_found")
                    owner = (
                        self.store.operation(
                            original.thread_id.removeprefix("phone-request:")
                        )
                        if original.thread_id.startswith("phone-request:")
                        else None
                    )
                    if (
                        owner is None
                        or owner.kind != "request_edit"
                        or owner.device_id != device_id
                        or not (original.resolves or original.amends)
                    ):
                        raise EditRejected(
                            422, "edit_not_undoable", "edit_not_undoable"
                        )
                    target_id = original.resolves or original.amends
                else:
                    target_id = edit.thread_id
                rows = decorate(canonical_threads(self.config, raw))
                target = next((t for t in rows if t.thread_id == target_id), None)
                if target is None:
                    raise EditRejected(404, "thread_not_found", "thread_not_found")
                if target.kind != "요청" or target.presentation != "fact":
                    raise EditRejected(422, "request_required", "request_required")
                if target.retracted:
                    raise EditRejected(409, "request_retracted", "request_retracted")
                if target.revision != edit.expected_revision:
                    raise EditRejected(409, "revision_conflict", "revision_conflict")
                if original is not None:
                    if any(t.retracts == original.thread_id for t in raw):
                        raise EditRejected(
                            409, "edit_already_undone", "edit_already_undone"
                        )
                    retracted = {t.retracts for t in raw if t.retracts}
                    if original.amends and any(
                        t.amends == target_id
                        and t.seq > original.seq
                        and t.thread_id not in retracted
                        for t in raw
                    ):
                        raise EditRejected(
                            409, "newer_deadline_change", "newer_deadline_change"
                        )
                elif not target.open:
                    raise EditRejected(
                        409, "request_already_resolved", "request_already_resolved"
                    )
                if isinstance(edit, DeadlineEdit):
                    try:
                        date.fromisoformat(edit.deadline)
                    except ValueError:
                        raise EditRejected(
                            422, "invalid_deadline", "invalid_deadline"
                        ) from None
                    if edit.deadline == target.effective_deadline:
                        result = {
                            "edit_thread_id": None,
                            "thread_id": target_id,
                            "revision": target.revision,
                            "changed": False,
                        }
                        return []
                # Undo is explicitly initiated by the person: actor stays human,
                # while its description is a server template, not user-written text.
                draft = ThreadDraft(
                    thread_id=new_id,
                    kind="결정",
                    actor="human",
                    content="폰에서 요청 처리를 되돌렸습니다"
                    if original
                    else edit.content
                    if isinstance(edit, RequestEdit)
                    else "",
                    occurred_at=stamp,
                    recorded_at=stamp,
                    source_id=source_id(operation),
                    tags=target.tags,
                    counterpart=target.counterpart,
                    direction=None,
                    field="deadline" if isinstance(edit, DeadlineEdit) else None,
                    value=edit.deadline if isinstance(edit, DeadlineEdit) else None,
                    amends=target_id if name == "deadline" else None,
                    resolves=target_id if name == "complete" else None,
                    retracts=original.thread_id if original else None,
                    correction=None,
                )
                return [draft]

            def run() -> dict[str, object]:
                made = FabricStore.from_config(self.config).edit(build)
                return result_for(made[0]) if made else result

            return self._finish(operation, run)
