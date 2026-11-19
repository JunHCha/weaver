"""Authenticated loopback edits using the existing transactional stores."""

import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Coroutine, Iterator
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from weaver.config import ServerConfig
from weaver.context import ContextDictionary, DictionarySnapshot, EntryAlreadyExists
from weaver.fabric import FabricStore, ThreadQuery
from weaver.runtime.loop import AlreadyRunning, InstanceLock
from weaver.schema import TagCorrection, Thread, ThreadDraft

from .presentation import presentation

MAX_BODY_BYTES = 65536
TagKind = Literal["project", "area", "person", "organization", "topic"]
Name = Annotated[str, Field(min_length=1, max_length=256)]


class EditRejected(HTTPException):
    """A refused edit with a stable code, shared by the web and mobile APIs."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(status_code, detail)
        self.code = code


class EditRoute(APIRoute):
    """Bound write bodies before parsing and translate only edit validation failures."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            if request.method in {"POST", "PATCH", "DELETE"}:
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise HTTPException(
                            413, "요청 본문은 64 KiB를 넘을 수 없습니다."
                        )
                    body.extend(chunk)

                async def receive() -> dict[str, Any]:
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }

                request = Request(request.scope, receive)
                try:
                    return await handler(request)
                except EntryAlreadyExists as error:
                    raise HTTPException(
                        422, f"이미 등록된 항목: {error.name}"
                    ) from None
                except ValueError:
                    raise HTTPException(
                        422,
                        "사전 편집 값을 확인하세요. 이름 또는 별칭이 충돌할 수 있습니다.",
                    ) from None
            return await handler(request)

        return bounded


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TagEdit(Input):
    add: list[Name] = Field(default_factory=list, max_length=100)
    remove: list[Name] = Field(default_factory=list, max_length=100)
    kinds: dict[Name, TagKind] = Field(default_factory=dict, max_length=100)


class BatchTagEdit(TagEdit):
    thread_ids: list[Name] = Field(min_length=1, max_length=500)


class DismissPair(Input):
    a: Name
    b: Name


class EntryEdit(Input):
    entry: Name
    name: Name | None = None
    kind: TagKind | None = None


class AliasEdit(Input):
    entry: Name
    alias: Name


class MergeEdit(Input):
    entry: Name
    target: Name
    kind: TagKind | None = None


@contextmanager
def writable_dictionary(config: ServerConfig) -> Iterator[ContextDictionary]:
    path = config.state_root / "context.sqlite3"
    version = None
    if path.is_file():
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)
        ) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
    # Hold the instance lock across a v1 upgrade, so a v1 daemon cannot start
    # between the version check and migration. v2 edits use SQLite transactions.
    lock = (
        InstanceLock(config.state_root / "weaver.lock")
        if version == 1
        else nullcontext()
    )
    try:
        with lock:
            yield ContextDictionary(config.state_root)
    except AlreadyRunning:
        raise HTTPException(
            503,
            "이전 사전을 사용하는 데몬이 실행 중입니다. 데몬을 종료한 뒤 다시 시도하세요.",
            headers={"Retry-After": "1"},
        ) from None


def dictionary_snapshot(config: ServerConfig) -> DictionarySnapshot:
    path = config.state_root / "context.sqlite3"
    return (
        ContextDictionary.open_readonly(path).snapshot()
        if path.is_file()
        else DictionarySnapshot([])
    )


def canonical_threads(config: ServerConfig, rows: list[Thread]) -> list[Thread]:
    dictionary = dictionary_snapshot(config)
    return [
        t.model_copy(
            update={
                "counterpart": (
                    entry.name
                    if t.counterpart and (entry := dictionary.resolve(t.counterpart))
                    else t.counterpart
                ),
                "tags": list(
                    dict.fromkeys(
                        entry.name if (entry := dictionary.resolve(tag)) else tag
                        for tag in t.tags
                    )
                ),
            }
        )
        for t in rows
    ]


def apply_tags(
    config: ServerConfig,
    ids: list[str],
    edit: TagEdit,
    *,
    new_id: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Correct tags of fact Threads; history Threads are rejected.

    ``new_id`` (target thread ID to correction ID) and ``now`` make the written
    corrections reproducible, so a mobile retry replays the same rows.
    """
    skipped: list[dict[str, str]] = []
    if any(not name.strip() for name in edit.add + edit.remove):
        raise EditRejected(422, "empty_tag", "태그 이름은 비어 있을 수 없습니다.")

    def build(rows: list[Thread], marked: set[str]) -> list[ThreadDraft]:
        by_id = {row.thread_id: row for row in rows}
        targets = []
        for id_ in dict.fromkeys(ids):
            target = by_id.get(id_)
            if target is None:
                raise EditRejected(
                    404, "thread_not_found", f"Thread를 찾을 수 없습니다: {id_}"
                )
            if presentation(target) == "history":
                raise EditRejected(
                    422,
                    "history_thread_readonly",
                    f"이력 Thread의 태그는 편집할 수 없습니다: {id_}",
                )
            targets.append(target)
        snapshot = dictionary_snapshot(config)

        def resolve(name: str) -> str:
            entry = snapshot.resolve(name)
            return entry.name if entry else unicodedata.normalize("NFC", name)

        if set(map(resolve, edit.add)) & set(map(resolve, edit.remove)):
            raise EditRejected(
                422,
                "tag_add_remove_conflict",
                "같은 태그를 동시에 추가하고 삭제할 수 없습니다.",
            )
        needed = {
            name
            for name in edit.add
            if any(
                resolve(name) not in {resolve(tag) for tag in target.tags}
                for target in targets
            )
        }
        for name in edit.add:
            existing = snapshot.resolve(name)
            if name in edit.kinds and existing:
                raise EditRejected(
                    422,
                    "entry_exists",
                    f"이미 등록된 항목: {existing.name} ({existing.kind}) — 사전을 새로 불러오세요.",
                )
            if name in needed and existing is None and name not in edit.kinds:
                raise EditRejected(
                    422, "kind_required", f"새 태그에는 종류가 필요합니다: {name}"
                )
        missing = [name for name in needed if snapshot.resolve(name) is None]
        if missing:
            with writable_dictionary(config) as dictionary:
                for name in missing:
                    dictionary.add_entry(
                        name, edit.kinds[name], human_edited=True, require_new=True
                    )
        snapshot = dictionary_snapshot(config)
        drafts = []
        for target in targets:
            effective = {resolve(tag) for tag in target.tags}
            additions = list(
                dict.fromkeys(
                    resolve(name) for name in edit.add if resolve(name) not in effective
                )
            )
            removing = set(map(resolve, edit.remove)) & effective
            removals = [name for name in target.tags if resolve(name) in removing]
            if not additions and not removals:
                skipped.append(
                    {
                        "thread_id": target.thread_id,
                        "reason": "이미 같은 태그 상태입니다.",
                    }
                )
                continue
            stamp = now or datetime.now(UTC)
            drafts.append(
                ThreadDraft(
                    thread_id=new_id(target.thread_id)
                    if new_id
                    else "thr_" + uuid4().hex,
                    kind="태그정정",
                    content="웹에서 태그 정정",
                    occurred_at=stamp,
                    recorded_at=stamp,
                    tags=[],
                    field=None,
                    value=None,
                    source_id=target.source_id,
                    actor="human",
                    direction=None,
                    resolves=None,
                    retracts=None,
                    correction=TagCorrection(
                        target=target.thread_id, add=additions, remove=removals
                    ),
                )
            )
        return drafts

    corrections = FabricStore.from_config(config).web_edit(build)
    return {
        "corrections": corrections,
        "changed": len(corrections),
        "skipped": len(skipped),
        "skipped_threads": skipped,
    }


def edit_entry(config: ServerConfig, edit: EntryEdit) -> dict[str, str]:
    entry = dictionary_snapshot(config).resolve(edit.entry)
    if entry is None:
        raise EditRejected(404, "entry_not_found", "사전 항목을 찾을 수 없습니다.")
    # Keep each request to one public, atomic dictionary transaction.
    if (edit.name is None) == (edit.kind is None):
        raise EditRejected(
            422, "one_change_required", "이름 또는 종류 중 하나만 변경하세요."
        )
    with writable_dictionary(config) as dictionary:
        if edit.name is not None:
            entry = dictionary.rename(edit.entry, edit.name)
        elif edit.kind is not None:
            entry = dictionary.set_kind(edit.entry, edit.kind)
    return {"name": entry.name}


def add_alias(config: ServerConfig, edit: AliasEdit) -> dict[str, str]:
    with writable_dictionary(config) as dictionary:
        return {"name": dictionary.add_alias(edit.entry, edit.alias).name}


def remove_alias(config: ServerConfig, edit: AliasEdit) -> dict[str, str]:
    with writable_dictionary(config) as dictionary:
        return {"name": dictionary.remove_alias(edit.entry, edit.alias).name}


def merge(config: ServerConfig, edit: MergeEdit) -> dict[str, str]:
    with writable_dictionary(config) as dictionary:
        snapshot = dictionary.snapshot()
        source, target = snapshot.resolve(edit.entry), snapshot.resolve(edit.target)
        if source and target and source.kind != target.kind and edit.kind is None:
            raise EditRejected(
                422,
                "kind_choice_required",
                "종류가 다른 항목은 합친 뒤의 종류를 직접 선택하세요.",
            )
        return {"name": dictionary.merge(edit.entry, edit.target, kind=edit.kind).name}


def dictionary_entries(config: ServerConfig) -> list[dict[str, object]]:
    """Dictionary entries with usage counts, shared by the web and phone APIs."""
    dictionary = dictionary_snapshot(config)
    path = config.state_root / "fabric.sqlite3"
    rows = (
        FabricStore.open_readonly(path).entries(ThreadQuery(include_executions=True))
        if path.is_file()
        else []
    )
    retracted = {t.retracts for t in rows if t.retracts}
    counts: dict[str, int] = {}
    latest: dict[str, datetime] = {}
    for thread in rows:
        if (
            thread.thread_id in retracted
            or thread.retracts
            or thread.kind in ("태그정정", "거절", "실행")
        ):
            continue
        names = {
            e.name if (e := dictionary.resolve(tag)) else tag for tag in thread.tags
        }
        for name in names:
            counts[name] = counts.get(name, 0) + 1
            latest[name] = max(latest.get(name, thread.occurred_at), thread.occurred_at)
    return [
        {
            "name": e.name,
            "kind": e.kind,
            "aliases": list(e.aliases),
            "human_edited": e.human_edited,
            "archived": e.archived,
            "promoted": e.promoted,
            "usage_count": counts.get(e.name, 0),
            "last_used_at": latest[e.name].isoformat() if e.name in latest else None,
        }
        for e in dictionary.entries
    ]


def editing_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(route_class=EditRoute)
    token = secrets.token_urlsafe(32)

    def authorize(request: Request) -> None:
        supplied = request.headers.get("x-weaver-token", "")
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source = origin if origin is not None else referer
        try:
            parsed = urlsplit(source or "")
            expected = urlsplit(str(request.base_url))
            # Next's development rewrite replaces Host and supplies the original
            # loopback authority in X-Forwarded-Host. Never allow a remote origin,
            # even with forwarding headers; the independent secret is mandatory.
            authorities = {expected.netloc}
            forwarded = request.headers.get("x-forwarded-host")
            if forwarded:
                proxy = urlsplit(f"{expected.scheme}://{forwarded}")
                if (
                    proxy.hostname in ("127.0.0.1", "localhost", "::1")
                    and proxy.netloc == forwarded
                    and proxy.username is None
                    and proxy.password is None
                ):
                    authorities.add(forwarded)
            valid_origin = (
                parsed.scheme in ("http", "https")
                and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                and parsed.scheme == expected.scheme
                and parsed.netloc in authorities
                and parsed.username is None
                and parsed.password is None
                and (
                    origin is None
                    or (
                        parsed.path in ("", "/")
                        and not parsed.query
                        and not parsed.fragment
                    )
                )
            )
        except ValueError:
            valid_origin = False
        if (
            not supplied.isascii()
            or not secrets.compare_digest(supplied, token)
            or not valid_origin
        ):
            raise HTTPException(
                403, "유효한 세션 토큰과 같은 loopback 출처가 필요합니다."
            )

    writes = [Depends(authorize)]

    @router.get("/api/session")
    def session() -> JSONResponse:
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})

    @router.get("/api/dictionary")
    def entries() -> list[dict[str, object]]:
        return dictionary_entries(config)

    @router.post("/api/threads/tags/batch", dependencies=writes)
    def batch_tags(edit: BatchTagEdit) -> dict[str, object]:
        return apply_tags(config, edit.thread_ids, edit)

    @router.post("/api/threads/{thread_id}/tags", dependencies=writes)
    def edit_tags(thread_id: str, edit: TagEdit) -> Thread:
        result = apply_tags(config, [thread_id], edit)
        corrections = result["corrections"]
        assert isinstance(corrections, list)
        if corrections:
            correction = corrections[0]
            assert isinstance(correction, Thread)
            return correction
        rows = FabricStore.from_config(config).entries(
            ThreadQuery(include_executions=True)
        )
        return canonical_threads(
            config, [next(t for t in rows if t.thread_id == thread_id)]
        )[0]

    @router.post("/api/corrections/{thread_id}/undo", dependencies=writes)
    def undo_tags(thread_id: str) -> Thread:
        previous: Thread | None = None

        def build(rows: list[Thread], marked: set[str]) -> list[ThreadDraft]:
            nonlocal previous
            target = next((t for t in rows if t.thread_id == thread_id), None)
            if target is None:
                raise HTTPException(404, "태그 정정을 찾을 수 없습니다.")
            if thread_id not in marked:
                raise HTTPException(
                    422, "웹 태그 편집기로 만든 정정만 되돌릴 수 있습니다."
                )
            previous = next((t for t in rows if t.retracts == thread_id), None)
            if previous:
                return []
            now = datetime.now(UTC)
            return [
                ThreadDraft(
                    thread_id="thr_" + uuid4().hex,
                    kind="기록",
                    content="웹 태그 정정 되돌리기",
                    occurred_at=now,
                    recorded_at=now,
                    tags=[],
                    field=None,
                    value=None,
                    source_id=target.source_id,
                    actor="human",
                    direction=None,
                    resolves=None,
                    retracts=thread_id,
                    correction=None,
                )
            ]

        result = FabricStore.from_config(config).web_edit(build)
        if previous:
            return previous
        return result[0]

    @router.get("/api/dictionary/dismissed")
    def dismissed() -> list[tuple[str, str]]:
        path = config.state_root / "context.sqlite3"
        return (
            ContextDictionary.open_readonly(path).dismissed_pairs()
            if path.is_file()
            else []
        )

    @router.post("/api/dictionary/dismissed", dependencies=writes)
    def dismiss(edit: DismissPair) -> dict[str, bool]:
        with writable_dictionary(config) as dictionary:
            dictionary.dismiss_pair(edit.a, edit.b)
        return {"dismissed": True}

    @router.patch("/api/dictionary", dependencies=writes)
    def patch_entry(edit: EntryEdit) -> dict[str, str]:
        return edit_entry(config, edit)

    @router.post("/api/dictionary/aliases", dependencies=writes)
    def post_alias(edit: AliasEdit) -> dict[str, str]:
        return add_alias(config, edit)

    @router.delete("/api/dictionary/aliases", dependencies=writes)
    def delete_alias(edit: AliasEdit) -> dict[str, str]:
        return remove_alias(config, edit)

    @router.post("/api/dictionary/merge", dependencies=writes)
    def post_merge(edit: MergeEdit) -> dict[str, str]:
        return merge(config, edit)

    return router
