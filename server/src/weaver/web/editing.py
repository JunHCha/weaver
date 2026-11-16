"""Authenticated loopback edits using the existing transactional stores."""

import secrets
import unicodedata
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from weaver.config import ServerConfig
from weaver.context import ContextDictionary, DictionarySnapshot
from weaver.fabric import FabricStore, ThreadQuery
from weaver.schema import TagCorrection, Thread, ThreadDraft

TagKind = Literal["project", "person", "organization", "topic"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TagEdit(Input):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    kinds: dict[str, TagKind] = Field(default_factory=dict)


class EntryEdit(Input):
    name: str | None = None
    kind: TagKind | None = None


class AliasEdit(Input):
    alias: str


class MergeEdit(Input):
    target: str


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
    router = APIRouter()
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
        if not edit.add and not edit.remove:
            raise HTTPException(422, "변경할 태그가 필요합니다.")
        if any(not name.strip() for name in edit.add + edit.remove):
            raise HTTPException(422, "태그 이름은 비어 있을 수 없습니다.")
        snapshot = dictionary_snapshot(config)
        for name in edit.add:
            if snapshot.resolve(name) is None and name not in edit.kinds:
                raise HTTPException(422, "새 태그에는 종류가 필요합니다.")

        def resolved_name(name: str) -> str:
            entry = snapshot.resolve(name)
            return entry.name if entry else unicodedata.normalize("NFC", name)

        if set(map(resolved_name, edit.add)).intersection(
            map(resolved_name, edit.remove)
        ):
            raise HTTPException(422, "같은 태그를 동시에 추가하고 삭제할 수 없습니다.")
        dictionary = ContextDictionary(config.state_root)
        additions = [
            (
                snapshot.resolve(name)
                or dictionary.add_entry(name, edit.kinds[name], human_edited=True)
            ).name
            for name in edit.add
        ]
        latest = dictionary.snapshot()

        def canonical(name: str) -> str:
            entry = latest.resolve(name)
            return entry.name if entry else unicodedata.normalize("NFC", name)

        removals = {canonical(name) for name in edit.remove}
        if removals.intersection(map(canonical, additions)):
            raise HTTPException(422, "같은 태그를 동시에 추가하고 삭제할 수 없습니다.")
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
                remove=[name for name in target.tags if canonical(name) in removals],
            ),
        )
        return FabricStore(path).append([draft])[0]

    @router.patch("/api/dictionary/{name}", dependencies=writes)
    def edit_entry(name: str, edit: EntryEdit) -> dict[str, str]:
        dictionary = ContextDictionary(config.state_root)
        if edit.name is None and edit.kind is None:
            raise HTTPException(422, "변경할 이름 또는 종류가 필요합니다.")
        # Pydantic validates both fields before either transactional edit.
        entry = dictionary.resolve(name)
        if entry is None:
            raise ValueError("unknown entry")
        if edit.name is not None:
            entry = dictionary.rename(name, edit.name)
        if edit.kind is not None:
            entry = dictionary.set_kind(entry.name, edit.kind)
        return {"name": entry.name}

    @router.post("/api/dictionary/{name}/aliases", dependencies=writes)
    def add_alias(name: str, edit: AliasEdit) -> dict[str, str]:
        return {
            "name": ContextDictionary(config.state_root)
            .add_alias(name, edit.alias)
            .name
        }

    @router.delete("/api/dictionary/{name}/aliases", dependencies=writes)
    def remove_alias(name: str, edit: AliasEdit) -> dict[str, str]:
        return {
            "name": ContextDictionary(config.state_root)
            .remove_alias(name, edit.alias)
            .name
        }

    @router.post("/api/dictionary/{name}/merge", dependencies=writes)
    def merge(name: str, edit: MergeEdit) -> dict[str, str]:
        return {
            "name": ContextDictionary(config.state_root).merge(name, edit.target).name
        }

    return router
