"""Document feedback from the phone: journaled by client_id like other edits."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from weaver.config import ServerConfig
from weaver.fabric import FabricStore
from weaver.schema.models import FeedbackChip
from weaver.web.editing import EditRejected
from weaver.wiki.feedback import (
    FeedbackAlreadyUndone,
    FeedbackNotFound,
    FeedbackStore,
    undo_feedback,
)

from .edits import _LOCK, ClientId, EditService
from .results import ResultModel
from .store import MobileStore, Operation

Text = Annotated[str, Field(strict=True, min_length=1, max_length=2000, pattern=r"\S")]
Ref = Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class FeedbackEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: ClientId
    document_id: Ref
    bullet_id: Ref | None = None
    text: Text
    chips: list[FeedbackChip] = []


class FeedbackUndo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: ClientId
    feedback_id: Annotated[str, Field(pattern=r"^fbk_[0-9a-f]{32}$")]


class FeedbackService(EditService):
    def __init__(
        self,
        config: ServerConfig,
        store: MobileStore,
        feedback: FeedbackStore,
        results: ResultModel,
    ) -> None:
        super().__init__(config, store)
        self.feedback, self.results = feedback, results

    def submit(self, device_id: str, edit: FeedbackEdit) -> tuple[Operation, bool]:
        # An unknown document is refused before anything is journaled.
        page_key = self.results.document_key(edit.document_id)
        with _LOCK:
            operation, _ = self._journal(
                device_id, "feedback", "submit", edit, edit.client_id
            )

            def run() -> dict[str, object]:
                item = self.feedback.submit(
                    document_id=edit.document_id,
                    page_key=page_key,
                    bullet_id=edit.bullet_id,
                    text=edit.text,
                    chips=list(edit.chips),
                    client_key=operation.operation_id,
                    now=datetime.fromisoformat(operation.created_at),
                )
                return item.model_dump(mode="json")

            return self._finish(operation, run)

    def undo(self, device_id: str, edit: FeedbackUndo) -> tuple[Operation, bool]:
        with _LOCK:
            operation, _ = self._journal(
                device_id, "feedback", "undo", edit, edit.client_id
            )

            def run() -> dict[str, object]:
                try:
                    item = undo_feedback(
                        self.feedback,
                        FabricStore.from_config(self.config),
                        edit.feedback_id,
                        datetime.fromisoformat(operation.created_at),
                        operation.operation_id,
                    )
                except FeedbackNotFound:
                    raise EditRejected(
                        404, "feedback_not_found", "feedback_not_found"
                    ) from None
                except FeedbackAlreadyUndone:
                    raise EditRejected(
                        409, "feedback_already_undone", "feedback_already_undone"
                    ) from None
                return item.model_dump(mode="json")

            return self._finish(operation, run)
