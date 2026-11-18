"""The phone-only HTTPS API, ``/mobile/v1`` (ADR-040, transport contract v1).

Separate from the loopback web app: its own ASGI app, its own listener, no
shared session token. Success of a write means only that the durable inbox or
archive step named in the contract is complete.
"""

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException

from weaver.action.courier import ActionCourier
from weaver.archive import ArchiveStore
from weaver.config import ServerConfig
from weaver.loom.summary_state import SummaryState
from weaver.schema import ActionResult

from .auth import PREFIX, AuthMiddleware, Principal
from .captures import CaptureService
from .edits import (
    EditService,
    MobileAliasEdit,
    MobileEntryEdit,
    MobileMergeEdit,
    MobileTagEdit,
)
from .errors import ApiError
from .feed import FeedProducer
from .read import ReadModel
from .reviews import ReviewEntry, ReviewService, etag
from .store import MobileStore, Operation, canonical, content_hash
from .whois import Whois

logger = logging.getLogger("weaver.mobile")
MAX_JSON_BYTES = 1 << 20
MAX_FEED_PAGE = 100
CURSOR = re.compile(r"^(ep_[0-9a-f]{32})\.(\d+)$")
TRANSPORT_VERSION = 1


def allowed_hosts(bind: str, port: int) -> frozenset[str]:
    host = f"[{bind}]" if ":" in bind else bind
    return frozenset({f"{host}:{port}"})


class Checkpoint(BaseModel):
    cursor: str


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "too_large")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise ApiError(413, "too_large")
    return bytes(data)


def operation_body(op: Operation) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": op.operation_id,
        "kind": op.kind,
        "status": op.status,
        "target_id": op.target_id,
        "target_revision": op.target_revision,
        "updated_at": op.updated_at,
    }
    if op.error is not None:
        body["error"] = op.error
    return body


def create_app(
    config: ServerConfig,
    whois: Whois,
    *,
    store: MobileStore | None = None,
    archive: ArchiveStore | None = None,
    summary: SummaryState | None = None,
    actions: ActionCourier | None = None,
) -> FastAPI:
    mobile = config.mobile
    if mobile.bind is None:
        raise ValueError("mobile.bind is not configured")
    store = store or MobileStore(config.state_root)
    archive = archive or ArchiveStore.from_config(config)
    summary = summary or SummaryState(config.state_root)
    reviews = ReviewService(summary, store)
    captures = CaptureService(
        archive,
        store,
        config.state_root / "mobile" / "staging",
        max_payload_bytes=mobile.max_payload_bytes,
        staging_days=mobile.staging_days,
    )
    read = ReadModel(config)
    edits = EditService(config, store)
    feed = FeedProducer(
        store,
        reviews,
        actions,
        read.head,
        action_dispatch=lambda: mobile.transport_for("action") == "http",
    )

    app = FastAPI(
        title="weaver-mobile", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(
        AuthMiddleware,
        store=store,
        whois=whois,
        allowed_hosts=allowed_hosts(mobile.bind, mobile.port),
    )

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
        return JSONResponse(error.body(), status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {"code": "invalid_request", "retryable": False}, status_code=422
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            error.status_code, "http_error"
        )
        return JSONResponse(
            {"code": code, "retryable": False}, status_code=error.status_code
        )

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, error: Exception) -> JSONResponse:
        logger.error("mobile request failed: %s", type(error).__name__)
        return JSONResponse(
            {"code": "internal_error", "retryable": True}, status_code=500
        )

    router = APIRouter(prefix=PREFIX)

    def principal(request: Request) -> Principal:
        found = request.scope["state"]["principal"]
        assert isinstance(found, Principal)
        return found

    Anyone = Annotated[Principal, Depends(principal)]

    def need(scope_name: str) -> Callable[[Anyone], Principal]:
        def check(who: Anyone) -> Principal:
            if scope_name not in who.device.scopes:
                raise ApiError(403, "scope_denied")
            return who

        return check

    CaptureScope = Annotated[Principal, Depends(need("capture"))]
    ReviewScope = Annotated[Principal, Depends(need("review"))]
    ReadScope = Annotated[Principal, Depends(need("read"))]
    EditScope = Annotated[Principal, Depends(need("edit"))]
    ActionScope = Annotated[Principal, Depends(need("action"))]

    def json_response(
        value: object, status: int = 200, headers: dict[str, str] | None = None
    ) -> JSONResponse:
        payload = jsonable_encoder(value)
        return JSONResponse(
            payload,
            status_code=status,
            headers={"Cache-Control": "no-store", **(headers or {})},
        )

    # ---- capabilities ----------------------------------------------------------

    @router.get("/capabilities")
    def capabilities(who: Anyone) -> JSONResponse:
        return json_response(
            {
                "server_id": store.server_id,
                "transport_version": TRANSPORT_VERSION,
                "schema_versions": {
                    "manifest": 1,
                    "receipt": 1,
                    "review_package": 2,
                    "verdict": 2,
                    "action": 2,
                    "action_result": 2,
                },
                "transports": {
                    feature: mobile.transport_for(feature)
                    for feature in ("capture", "review", "action")
                },
                "limits": {
                    "max_payload_bytes": mobile.max_payload_bytes,
                    "max_json_bytes": MAX_JSON_BYTES,
                    "max_feed_items": MAX_FEED_PAGE,
                    "max_feed_bytes": 4 * 1024 * 1024,
                },
                "feed_epoch": store.feed_epoch,
                "device": {
                    "device_id": who.device.device_id,
                    "scopes": sorted(who.device.scopes),
                    "executor": who.device.executor,
                },
            }
        )

    # ---- captures --------------------------------------------------------------

    @router.put("/captures/{capture_id}/payload")
    async def put_payload(
        capture_id: str, request: Request, who: CaptureScope
    ) -> JSONResponse:
        declared = request.headers.get("content-length")
        length = int(declared) if declared and declared.isdigit() else None
        status, body = await captures.put_payload(
            capture_id,
            who.device.device_id,
            length,
            request.headers.get("x-content-sha256"),
            _stream(request),
        )
        return json_response(body, status)

    @router.put("/captures/{capture_id}/manifest")
    async def put_manifest(
        capture_id: str, request: Request, _who: CaptureScope
    ) -> JSONResponse:
        raw = await _body(request, 64 * 1024)
        status, receipt = captures.put_manifest(capture_id, raw)
        return json_response(receipt, status)

    @router.get("/captures/{capture_id}/receipt")
    def get_receipt(capture_id: str, _who: CaptureScope) -> JSONResponse:
        return json_response(captures.receipt(capture_id))

    # ---- reviews ---------------------------------------------------------------

    @router.get("/reviews")
    def list_reviews(_who: ReviewScope) -> JSONResponse:
        return json_response(
            {"items": [ReviewEntry(p).summary for p in reviews.open_packages()]}
        )

    @router.get("/reviews/{package_id}/revisions/{revision}")
    def get_review(package_id: str, revision: int, _who: ReviewScope) -> JSONResponse:
        package = reviews.package(package_id, revision)
        return json_response(package, headers={"ETag": etag(package)})

    @router.put("/reviews/{package_id}/revisions/{revision}/verdict")
    async def put_verdict(
        package_id: str,
        revision: int,
        request: Request,
        who: ReviewScope,
    ) -> JSONResponse:
        raw = await _body(request, MAX_JSON_BYTES)
        operation, _created = reviews.submit(
            who.device.device_id,
            package_id,
            revision,
            request.headers.get("if-match"),
            raw,
        )
        return json_response(
            operation_body(operation),
            202,
            {"Location": f"{PREFIX}/operations/{operation.operation_id}"},
        )

    @router.get("/operations/{operation_id}")
    def get_operation(operation_id: str, who: Anyone) -> JSONResponse:
        operation = store.operation(operation_id)
        if operation is None or operation.device_id != who.device.device_id:
            raise ApiError(404, "operation_not_found")
        return json_response(operation_body(operation))

    # ---- actions ---------------------------------------------------------------

    def executor_only(who: ActionScope) -> Principal:
        if not who.device.executor:
            raise ApiError(403, "not_executor")
        return who

    Executor = Annotated[Principal, Depends(executor_only)]

    @router.get("/actions")
    def list_actions(_who: Executor) -> JSONResponse:
        if actions is None or mobile.transport_for("action") != "http":
            raise ApiError(409, "transport_files")
        return json_response({"items": actions.dispatchable()})

    @router.get("/actions/{action_id}")
    def get_action(action_id: str, _who: Executor) -> JSONResponse:
        if actions is None or mobile.transport_for("action") != "http":
            raise ApiError(409, "transport_files")
        for action in actions.dispatchable():
            if action.action_id == action_id:
                return json_response(action)
        raise ApiError(404, "action_not_found")

    @router.put("/actions/{action_id}/result")
    async def put_result(
        action_id: str, request: Request, who: Executor
    ) -> JSONResponse:
        if actions is None:
            raise ApiError(503, "actions_unavailable", retryable=True)
        raw = await _body(request, MAX_JSON_BYTES)
        try:
            result = ActionResult.model_validate_json(raw)
        except ValidationError:
            raise ApiError(422, "invalid_result") from None
        if result.action_id != action_id:
            raise ApiError(422, "id_mismatch")
        record = actions.outbox.get(action_id)
        if record is None:
            raise ApiError(404, "unknown_action")
        digest = content_hash(canonical(result.model_dump(mode="json")))
        operation, _created = store.enqueue(
            "action_result",
            who.device.device_id,
            action_id,
            digest,
            action_id,
            None,
            result.model_dump_json(),
        )
        if operation.content_hash != digest:
            raise ApiError(409, "result_conflict")
        return json_response(
            operation_body(operation),
            202,
            {"Location": f"{PREFIX}/operations/{operation.operation_id}"},
        )

    # ---- change feed -----------------------------------------------------------

    @router.get("/changes")
    def changes(
        who: Anyone,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_FEED_PAGE)] = MAX_FEED_PAGE,
    ) -> JSONResponse:
        feed.reconcile()
        epoch = store.feed_epoch
        after = 0
        if cursor is not None:
            match = CURSOR.fullmatch(cursor)
            if match is None:
                raise ApiError(422, "invalid_cursor")
            if match.group(1) != epoch or int(match.group(2)) > store.feed_head():
                raise ApiError(410, "cursor_expired")
            after = int(match.group(2))
        visible = {"review": "review", "action": "action", "fabric": "read"}
        items, more = store.feed_page(after, limit)
        shown = [
            {
                "seq": item.seq,
                "kind": item.kind,
                "id": item.resource_id,
                "revision": item.revision,
                "op": item.op,
                "body": None if item.body is None else json.loads(item.body),
            }
            for item in items
            if visible.get(item.kind) in who.device.scopes
        ]
        last = items[-1].seq if items else after
        return json_response(
            {
                "feed_epoch": epoch,
                "items": shown,
                "next_cursor": f"{epoch}.{last}",
                "has_more": more,
            }
        )

    @router.put("/devices/self/checkpoint")
    async def checkpoint(request: Request, who: Anyone) -> JSONResponse:
        try:
            body = Checkpoint.model_validate_json(await _body(request, 1024))
        except ValidationError:
            raise ApiError(422, "invalid_cursor") from None
        match = CURSOR.fullmatch(body.cursor)
        if match is None:
            raise ApiError(422, "invalid_cursor")
        if (
            match.group(1) != store.feed_epoch
            or int(match.group(2)) > store.feed_head()
        ):
            raise ApiError(410, "cursor_expired")
        store.set_checkpoint(who.device.device_id, match.group(1), int(match.group(2)))
        return json_response({"cursor": body.cursor})

    # ---- read model ------------------------------------------------------------

    @router.get("/read/now")
    def read_now(_who: ReadScope) -> JSONResponse:
        return json_response(read.now())

    @router.get("/read/threads")
    def read_threads(
        _who: ReadScope,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> JSONResponse:
        return json_response(read.threads(after_seq, limit))

    @router.get("/read/threads/{thread_id}")
    def read_thread(thread_id: str, _who: ReadScope) -> JSONResponse:
        return json_response(read.thread(thread_id))

    @router.get("/read/tags")
    def read_tags(_who: ReadScope) -> JSONResponse:
        return json_response([t.model_dump() for t in read.tags()])

    @router.get("/read/states")
    def read_states(
        _who: ReadScope,
        tag: Annotated[list[str] | None, Query()] = None,
    ) -> JSONResponse:
        return json_response(read.states(tag))

    @router.get("/read/timeline")
    def read_timeline(
        subject: str,
        _who: ReadScope,
        mentions: bool = False,
        history: bool = False,
    ) -> JSONResponse:
        return json_response(
            [
                r.model_dump(mode="json")
                for r in read.timeline(subject, mentions, history)
            ]
        )

    @router.get("/read/wiki/pages")
    def read_wiki_pages(_who: ReadScope) -> JSONResponse:
        return json_response([p.model_dump(mode="json") for p in read.wiki_pages()])

    @router.get("/read/wiki/pages/{page_id}")
    def read_wiki_page(page_id: str, request: Request, _who: ReadScope) -> Response:
        page = read.wiki_page(page_id)
        tag = f'"{page.sha256}"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
        return json_response(page, headers={"ETag": tag})

    # ---- edits (journaled by client_id) ----------------------------------------

    def edit_response(operation: Operation, replay: bool) -> JSONResponse:
        return json_response(
            {
                **operation_body(operation),
                "result": json.loads(operation.result) if operation.result else None,
            },
            200,
            {"Idempotent-Replay": "true"} if replay else None,
        )

    async def parse[T: BaseModel](request: Request, model: type[T]) -> T:
        raw = await _body(request, 64 * 1024)
        try:
            return model.model_validate_json(raw)
        except ValidationError:
            raise ApiError(422, "invalid_request") from None

    @router.post("/edits/tags")
    async def edit_tags(request: Request, who: EditScope) -> JSONResponse:
        edit = await parse(request, MobileTagEdit)
        return edit_response(*await _in_thread(edits.tags, who.device.device_id, edit))

    @router.post("/edits/dictionary/{operation}")
    async def edit_dictionary(
        operation: str, request: Request, who: EditScope
    ) -> JSONResponse:
        models: dict[str, type[BaseModel]] = {
            "rename": MobileEntryEdit,
            "kind": MobileEntryEdit,
            "alias-add": MobileAliasEdit,
            "alias-remove": MobileAliasEdit,
            "merge": MobileMergeEdit,
        }
        model = models.get(operation)
        if model is None:
            raise ApiError(404, "not_found")
        edit = await parse(request, model)
        client_id: str = edit.client_id  # type: ignore[attr-defined]
        return edit_response(
            *await _in_thread(
                edits.dictionary, who.device.device_id, operation, edit, client_id
            )
        )

    app.include_router(router)
    app.state.store = store
    return app


async def _stream(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        yield chunk


async def _in_thread(function: Any, *args: Any) -> Any:
    import anyio.to_thread

    return await anyio.to_thread.run_sync(function, *args)
