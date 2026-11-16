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
from weaver.context import ContextDictionary, DictionarySnapshot
from weaver.fabric import FabricStore, ThreadQuery
from weaver.runtime.loop import AlreadyRunning, InstanceLock
from weaver.schema import TagCorrection, Thread, ThreadDraft

MAX_BODY_BYTES = 65536
TagKind = Literal["project", "person", "organization", "topic"]
Name = Annotated[str, Field(min_length=1, max_length=256)]


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
                "tags": list(
                    dict.fromkeys(
                        entry.name if (entry := dictionary.resolve(tag)) else tag
                        for tag in t.tags
                    )
                )
            }
        )
        for t in rows
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
        dictionary = dictionary_snapshot(config)
        path = config.state_root / "fabric.sqlite3"
        rows = (
            FabricStore.open_readonly(path).entries(
                ThreadQuery(include_executions=True)
            )
            if path.is_file()
            else []
        )
        retracted = {t.retracts for t in rows if t.retracts}
        counts: dict[str, int] = {}
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
        return [
            {
                "name": e.name,
                "kind": e.kind,
                "aliases": list(e.aliases),
                "human_edited": e.human_edited,
                "usage_count": counts.get(e.name, 0),
            }
            for e in dictionary.entries
        ]

    @router.post("/api/threads/{thread_id}/tags", dependencies=writes)
    def edit_tags(thread_id: str, edit: TagEdit) -> Thread:
        path = config.state_root / "fabric.sqlite3"
        rows = (
            FabricStore.open_readonly(path).entries(
                ThreadQuery(include_executions=True)
            )
            if path.is_file()
            else []
        )
        target = next((t for t in rows if t.thread_id == thread_id), None)
        if target is None:
            raise HTTPException(404, "Thread를 찾을 수 없습니다.")
        if target.kind == "실행":
            raise HTTPException(422, "실행 기록의 태그는 편집할 수 없습니다.")
        if any(not name.strip() for name in edit.add + edit.remove):
            raise HTTPException(422, "태그 이름은 비어 있을 수 없습니다.")
        snapshot = dictionary_snapshot(config)

        def resolved_name(name: str) -> str:
            entry = snapshot.resolve(name)
            return entry.name if entry else unicodedata.normalize("NFC", name)

        if set(map(resolved_name, edit.add)).intersection(
            map(resolved_name, edit.remove)
        ):
            raise HTTPException(422, "같은 태그를 동시에 추가하고 삭제할 수 없습니다.")
        effective = set(map(resolved_name, target.tags))
        add_names = list(
            dict.fromkeys(
                name for name in edit.add if resolved_name(name) not in effective
            )
        )
        remove_names = {resolved_name(name) for name in edit.remove} & effective
        if not add_names and not remove_names:
            return target.model_copy(
                update={"tags": list(dict.fromkeys(map(resolved_name, target.tags)))}
            )
        for name in add_names:
            if snapshot.resolve(name) is None and name not in edit.kinds:
                raise HTTPException(422, "새 태그에는 종류가 필요합니다.")
        with writable_dictionary(config) as dictionary:
            additions = [
                (
                    snapshot.resolve(name)
                    or dictionary.add_entry(name, edit.kinds[name], human_edited=True)
                ).name
                for name in add_names
            ]
        removals = [name for name in target.tags if resolved_name(name) in remove_names]
        now = datetime.now(UTC)
        draft = ThreadDraft(
            thread_id="thr_" + uuid4().hex,
            kind="태그정정",
            content="웹에서 태그 정정",
            occurred_at=now,
            recorded_at=now,
            tags=[],
            field=None,
            value=None,
            source_id=target.source_id,
            actor="human",
            direction=None,
            resolves=None,
            retracts=None,
            correction=TagCorrection(
                target=thread_id,
                add=additions,
                remove=removals,
            ),
        )
        return FabricStore(path).append([draft])[0]

    @router.patch("/api/dictionary", dependencies=writes)
    def edit_entry(edit: EntryEdit) -> dict[str, str]:
        entry = dictionary_snapshot(config).resolve(edit.entry)
        if entry is None:
            raise HTTPException(404, "사전 항목을 찾을 수 없습니다.")
        # Keep each request to one public, atomic dictionary transaction.
        if (edit.name is None) == (edit.kind is None):
            raise HTTPException(422, "이름 또는 종류 중 하나만 변경하세요.")
        with writable_dictionary(config) as dictionary:
            if edit.name is not None:
                entry = dictionary.rename(edit.entry, edit.name)
            elif edit.kind is not None:
                entry = dictionary.set_kind(edit.entry, edit.kind)
        return {"name": entry.name}

    @router.post("/api/dictionary/aliases", dependencies=writes)
    def add_alias(edit: AliasEdit) -> dict[str, str]:
        with writable_dictionary(config) as dictionary:
            return {"name": dictionary.add_alias(edit.entry, edit.alias).name}

    @router.delete("/api/dictionary/aliases", dependencies=writes)
    def remove_alias(edit: AliasEdit) -> dict[str, str]:
        with writable_dictionary(config) as dictionary:
            return {"name": dictionary.remove_alias(edit.entry, edit.alias).name}

    @router.post("/api/dictionary/merge", dependencies=writes)
    def merge(edit: MergeEdit) -> dict[str, str]:
        with writable_dictionary(config) as dictionary:
            return {"name": dictionary.merge(edit.entry, edit.target).name}

    return router
