"""Read-only local HTTP views of Fabric and runtime observations.

Normal SQLite WAL sidecars (-wal/-shm) are an exception to file creation;
the web command sets umask 077 and never modifies original main database files.
"""

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, JsonValue, ValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Scope

from weaver.config import ServerConfig
from weaver.context import ContextDictionary
from weaver.context import SchemaVersionError as ContextSchemaError
from weaver.fabric import FabricStore, ThreadQuery
from weaver.fabric.state import project
from weaver.fabric.store import SchemaVersionError as FabricSchemaError
from weaver.schema import PipelineItem, Thread

logger = logging.getLogger(__name__)

Stage = Literal["spinner", "loom"]
_LOG = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)?) "
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) ([\w.]+): (.*)$"
)
_SOURCE = re.compile(r"\bsrc_[A-Za-z0-9]+\b")


class LogLine(BaseModel):
    ts: str
    lv: str
    lg: str
    msg: str
    src: str | None


class SourceView(BaseModel):
    id: str
    kind: str
    status: str
    updated_at: str
    stage_times: dict[str, str]


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    """Read runtime databases without creating files, migrating or chmodding."""
    if not path.is_file():
        return []
    try:
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)
        ) as db:
            return list(db.execute(sql))
    except sqlite3.OperationalError as error:
        if str(error).startswith("no such table:"):
            return []
        raise


def sources(
    config: ServerConfig, stage: Stage, *, limit: int | None = 500
) -> list[SourceView]:
    ledger = {
        str(sid): (str(status), str(at))
        for sid, status, at in _rows(
            config.state_root / "runtime.sqlite3",
            "SELECT source_id, status, updated_at FROM sources",
        )
    }
    result = []
    for (raw,) in _rows(
        config.state_root / "pipeline.sqlite3", "SELECT payload FROM items"
    ):
        try:
            item = PipelineItem.model_validate_json(str(raw))
        except ValidationError:
            logger.warning("invalid pipeline observation skipped")
            continue
        if item.source_type == "wiki_edit":
            continue
        if stage == "loom" and "loom" not in item.history:
            continue
        status = "처리 중"
        if stage == "spinner" and item.location != "spinner":
            status = "완료"
        elif item.status in ("done", "rejected"):
            status = "완료"
        elif item.status == "review":
            status = "검토 대기"
        elif item.status == "apply_stopped":
            status = "중지됨"
        activity = item.updated_at
        if stage == "spinner" and item.location != "spinner":
            activity = item.stage_times.get(
                "정리",
                max(
                    (v for k, v in item.stage_times.items() if k in ("수집", "전송")),
                    default=item.updated_at,
                ),
            )
        at = activity.astimezone(config.time.zone).isoformat()
        progress = ledger.get(item.source_id)
        if progress and (stage == "loom" or item.location == "spinner"):
            code, ledger_at = progress
            if code in ("retry", "failed", "waiting_llm"):
                status = {
                    "retry": "재시도 대기",
                    "failed": "중지됨",
                    "waiting_llm": "처리 중",
                }[code]
                at = (
                    max(datetime.fromisoformat(at), datetime.fromisoformat(ledger_at))
                    .astimezone(config.time.zone)
                    .isoformat()
                )
        result.append(
            SourceView(
                id=item.source_id,
                kind={
                    "call": "통화",
                    "voice_memo": "음성 메모",
                    "share_text": "공유 텍스트",
                    "kakao": "공유 텍스트",
                }[item.source_type],
                status=status,
                updated_at=at,
                stage_times={
                    key: value.isoformat() for key, value in item.stage_times.items()
                },
            )
        )
    ordered = sorted(
        result, key=lambda item: datetime.fromisoformat(item.updated_at), reverse=True
    )
    return ordered if limit is None else ordered[:limit]


def _reverse_lines(path: Path) -> Iterator[str]:
    """Tail bounded chunks; do not read older rotations unless filters need them."""
    with path.open("rb") as stream:
        position = stream.seek(0, 2)
        pending = b""
        while position:
            size = min(position, 65536)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + pending).split(b"\n")
            pending = lines.pop(0)
            for line in reversed(lines):
                yield line.decode("utf-8", errors="replace").rstrip("\r")
        if pending:
            yield pending.decode("utf-8", errors="replace").rstrip("\r")


def read_logs(
    config: ServerConfig,
    component: Stage,
    level: str = "",
    source_id: str = "",
    limit: int = 500,
) -> list[LogLine]:
    result: list[LogLine] = []
    prefix = f"weaver.{component}"
    for suffix in ("", ".1", ".2", ".3", ".4", ".5"):
        path = config.log_root / f"weaver.log{suffix}"
        if not path.is_file():
            continue
        for line in _reverse_lines(path):
            match = _LOG.match(line)
            if not match:
                continue
            ts, lv, lg, msg = match.groups()
            if lg != prefix and not lg.startswith(prefix + "."):
                continue
            if level and lv != level:
                continue
            ids = _SOURCE.findall(msg)
            if source_id and source_id not in ids:
                continue
            result.append(
                LogLine(ts=ts, lv=lv, lg=lg, msg=msg, src=ids[0] if ids else None)
            )
            if len(result) >= limit:
                return list(reversed(result))
    return list(reversed(result))


class ExportFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
            if response.status_code != 404:
                return response
        except HTTPException as error:
            if error.status_code != 404:
                raise
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404)
        root = Path(str(self.directory)).resolve()
        page = (root / f"{path.rstrip('/')}.html").resolve()
        if not page.is_relative_to(root):
            raise HTTPException(404)
        if not Path(path).suffix and page.is_file():
            return FileResponse(page)
        not_found = root / "404.html"
        if not_found.is_file():
            return FileResponse(not_found, status_code=404)
        raise HTTPException(404)


def create_app(config: ServerConfig, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="weaver", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
        www_redirect=False,
    )

    @app.exception_handler(FabricSchemaError)
    @app.exception_handler(ContextSchemaError)
    async def schema_error(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            {
                "detail": "Unsupported state database schema; update Weaver before viewing this state."
            },
            status_code=503,
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def state_unavailable(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": "State database is temporarily unavailable."}, status_code=503
        )

    def snapshot(include_executions: bool = False) -> list[Thread]:
        if not (config.state_root / "fabric.sqlite3").is_file():
            return []
        return [
            t.model_copy(
                update={
                    "occurred_at": t.occurred_at.astimezone(config.time.zone),
                    "recorded_at": t.recorded_at.astimezone(config.time.zone),
                }
            )
            for t in FabricStore.open_readonly(
                config.state_root / "fabric.sqlite3"
            ).entries(ThreadQuery(include_executions=include_executions))
        ]

    @app.get("/api/threads")
    def threads(include_executions: bool = False) -> list[Thread]:
        return snapshot(include_executions)

    @app.get("/api/tags")
    def tags() -> list[dict[str, str | None]]:
        names = sorted({tag for thread in snapshot(True) for tag in thread.tags})
        context = (
            ContextDictionary.open_readonly(config.state_root / "context.sqlite3")
            if (config.state_root / "context.sqlite3").is_file()
            else None
        )
        dictionary = context.snapshot() if context else None
        result: list[dict[str, str | None]] = []
        for name in names:
            entry = dictionary.resolve(name) if dictionary else None
            result.append({"name": name, "kind": entry.kind if entry else None})
        return result

    @app.get("/api/states")
    def states(
        tag: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, JsonValue]:
        full = snapshot(True)
        result: dict[str, JsonValue] = {}
        for name in tag or sorted({tag for t in full for tag in t.tags}):
            state = project(name, full)
            result[name] = {
                **{
                    key: state.fields[key].value if key in state.fields else None
                    for key in ("deadline", "schedule", "status")
                },
                "assignees": [str(name) for name in sorted(state.assignees)],
                "open_requests": [t.thread_id for t in state.open_requests],
            }
        return result

    @app.get("/api/sources")
    def source_list(stage: Stage) -> list[SourceView]:
        return sources(config, stage)

    @app.get("/api/logs")
    def logs(
        component: Stage,
        level: str = "",
        source_id: str = "",
        limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    ) -> list[LogLine]:
        return read_logs(config, component, level, source_id, limit)

    @app.get("/api/status")
    def status() -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for stage in ("spinner", "loom"):
            rows = sources(config, stage, limit=None)
            logs = read_logs(config, stage, limit=1)
            times = [datetime.fromisoformat(item.updated_at) for item in rows]
            times.extend(
                datetime.strptime(line.ts, "%Y-%m-%d %H:%M:%S,%f").astimezone()
                if "," in line.ts
                else datetime.strptime(line.ts, "%Y-%m-%d %H:%M:%S").astimezone()
                for line in logs
            )
            latest = max(times, default=None)
            result[stage] = {
                "last_activity": latest.astimezone(config.time.zone).isoformat()
                if latest
                else None,
                "waiting_review": sum(item.status == "검토 대기" for item in rows),
            }
        return result

    if static_dir is not None:
        app.mount("/", ExportFiles(directory=static_dir, html=True))
    return app
