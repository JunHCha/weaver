"""Tag and dictionary edits for the phone, reusing the web edit functions.

The rules (history Threads are read-only, mention/alias handling, atomic
dictionary transactions) live in ``weaver.web.editing``; this module only adds
the client-ID journal that makes a retried request return the original result.
"""

import hashlib
import json
import threading
from datetime import datetime
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from weaver.config import ServerConfig
from weaver.context import EntryAlreadyExists
from weaver.fabric import FabricStore, ThreadQuery
from weaver.web.editing import (
    AliasEdit,
    BatchTagEdit,
    EditRejected,
    EntryEdit,
    MergeEdit,
    add_alias,
    apply_tags,
    edit_entry,
    merge,
    remove_alias,
)

from .errors import ApiError
from .store import MobileStore, Operation, canonical, content_hash

ClientId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
_LOCK = threading.Lock()


class MobileTagEdit(BatchTagEdit):
    client_id: ClientId


class MobileEntryEdit(EntryEdit):
    client_id: ClientId


class MobileAliasEdit(AliasEdit):
    client_id: ClientId


class MobileMergeEdit(MergeEdit):
    client_id: ClientId


class EditService:
    def __init__(self, config: ServerConfig, store: MobileStore) -> None:
        self.config = config
        self.store = store

    def _journal(
        self, device_id: str, kind: str, name: str, body: BaseModel, client_id: str
    ) -> tuple[Operation, bool]:
        payload = body.model_dump(mode="json", exclude={"client_id"})
        digest = content_hash(canonical({"op": name, "body": payload}))
        operation, created = self.store.enqueue(
            "tag_edit" if kind == "tags" else "dictionary_edit",
            device_id,
            client_id,
            digest,
            name,
            None,
            json.dumps(payload),
        )
        if operation.content_hash != digest:
            raise ApiError(409, "client_id_conflict")
        return operation, created

    def _finish(self, operation: Operation, run: Any) -> tuple[Operation, bool]:
        if operation.status == "applied":
            return operation, True
        if operation.status == "failed":
            raise ApiError(
                int(operation.result or 422), operation.error or "edit_failed"
            )
        try:
            result = run()
        except EditRejected as error:
            self.store.update_operation(
                operation.operation_id,
                "failed",
                error=error.code,
                result=str(error.status_code),
                drop_payload=True,
            )
            raise ApiError(error.status_code, error.code) from None
        except HTTPException as error:
            code = "dictionary_busy" if error.status_code == 503 else "edit_failed"
            raise ApiError(
                error.status_code, code, retryable=error.status_code == 503
            ) from None
        except (ValueError, EntryAlreadyExists):
            self.store.update_operation(
                operation.operation_id,
                "failed",
                error="invalid_edit",
                result="422",
                drop_payload=True,
            )
            raise ApiError(422, "invalid_edit") from None
        self.store.update_operation(
            operation.operation_id,
            "applied",
            result=json.dumps(result, ensure_ascii=False),
            drop_payload=True,
        )
        done = self.store.operation(operation.operation_id)
        assert done is not None
        return done, False

    def tags(self, device_id: str, edit: MobileTagEdit) -> tuple[Operation, bool]:
        with _LOCK:
            operation, _ = self._journal(
                device_id, "tags", "tags", edit, edit.client_id
            )

            def new_id(target: str) -> str:
                return (
                    "thr_"
                    + hashlib.sha256(f"{edit.client_id}:{target}".encode()).hexdigest()[
                        :32
                    ]
                )

            def run() -> dict[str, object]:
                plain = BatchTagEdit(**edit.model_dump(exclude={"client_id"}))
                result = apply_tags(
                    self.config,
                    plain.thread_ids,
                    plain,
                    new_id=new_id,
                    now=datetime.fromisoformat(operation.created_at),
                )
                # A replay (the first answer was lost) finds its own rows already
                # written; report them by their deterministic IDs, not as skipped.
                existing = {
                    t.thread_id
                    for t in FabricStore.from_config(self.config).entries(
                        ThreadQuery(include_executions=True)
                    )
                }
                targets = list(dict.fromkeys(plain.thread_ids))
                made = [new_id(t) for t in targets if new_id(t) in existing]
                skipped = [
                    s
                    for s in result["skipped_threads"]  # type: ignore[attr-defined]
                    if new_id(s["thread_id"]) not in existing
                ]
                return {
                    "correction_ids": made,
                    "changed": len(made),
                    "skipped": len(skipped),
                    "skipped_threads": skipped,
                }

            return self._finish(operation, run)

    def dictionary(
        self, device_id: str, name: str, edit: BaseModel, client_id: str
    ) -> tuple[Operation, bool]:
        with _LOCK:
            operation, _ = self._journal(device_id, "dictionary", name, edit, client_id)
            raw = edit.model_dump(exclude={"client_id"})

            def run() -> dict[str, object]:
                if name in ("rename", "kind"):
                    return dict(edit_entry(self.config, EntryEdit(**raw)))
                if name == "alias-add":
                    return dict(add_alias(self.config, AliasEdit(**raw)))
                if name == "alias-remove":
                    return dict(remove_alias(self.config, AliasEdit(**raw)))
                return dict(merge(self.config, MergeEdit(**raw)))

            return self._finish(operation, run)
