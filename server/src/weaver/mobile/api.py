"""Capture and phone API, ``/mobile/v1`` (ADR-040/044, transport contract v1).

The tailnet HTTPS and Shepherd loopback listeners use separate ASGI apps and
authentication modes. Neither shares the loopback web app's session token. Success of a write means only that the durable inbox or
archive step named in the contract is complete.
"""

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException

from weaver.action.courier import ActionCourier
from weaver.alert.delivery import AlertDelivery
from weaver.alert.history import AlertBodyExpired
from weaver.archive import ArchiveStore
from weaver.config import ServerConfig
from weaver.loom.summary_state import SummaryState
from weaver.schema import ActionResult
from weaver.wiki.feedback import FeedbackStore

from .auth import PREFIX, AuthMiddleware, Principal
from .captures import CaptureService
from .crashes import CrashStore
from .edits import (
    EditService,
    MobileAliasEdit,
    MobileEntryEdit,
    MobileMergeEdit,
    MobileTagEdit,
)
from .errors import ApiError
from .feed import KIND_SCOPES, LEGACY_KINDS, FeedProducer, FeedSources
from .feedback import FeedbackEdit, FeedbackService, FeedbackUndo
from .pipeline import PipelineReadModel
from .read import ReadModel
from .requests import DeadlineEdit, RequestEdit, RequestEditService, UndoEdit
from .results import ResultModel
from .reviews import ReviewEntry, ReviewService, etag
from .store import MobileStore, Operation, canonical, content_hash
from .whois import Whois

logger = logging.getLogger("weaver.mobile")
MAX_JSON_BYTES = 1 << 20
MAX_FEED_PAGE = 100
MAX_FEED_BYTES = 4 * 1024 * 1024
CURSOR = re.compile(r"^(ep_[0-9a-f]{32})\.(\d+)(?:\.([0-9a-f]{8}))?$")
TRANSPORT_VERSION = 1


def kinds_pin(kinds: frozenset[str]) -> str:
    """Short digest of the requested kind set, carried inside the feed cursor."""
    return hashlib.sha256(",".join(sorted(kinds)).encode()).hexdigest()[:8]


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


_STOPPED = {"apply_stopped", "payload_lost"}  # failed, not refused
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def utc_z(value: object) -> object:
    """Rewrite every datetime string as UTC ending in ``Z`` (not frontmatter)."""
    if isinstance(value, dict):
        return {k: v if k == "frontmatter" else utc_z(v) for k, v in value.items()}
    if isinstance(value, list):
        return [utc_z(item) for item in value]
    if isinstance(value, str) and _DATETIME.fullmatch(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        text = parsed.isoformat(
            timespec="seconds" if not parsed.microsecond else "microseconds"
        )
        return text.removesuffix("+00:00") + "Z"
    return value


def operation_body(op: Operation) -> dict[str, object]:
    """``{operation_id, state, code?, kind, target_id, target_revision, updated_at}``.

    ``state``: ``queued`` (accepted or still being applied, ``code`` says why it
    waits), ``applied``, ``rejected`` (refused by the domain; do not resend) or
    ``failed`` (stopped; needs the operator).
    """
    state = {"queued": "queued", "applying": "queued", "applied": "applied"}.get(
        op.status, ""
    )
    if not state:
        state = (
            "failed" if op.status == "failed" and op.error in _STOPPED else "rejected"
        )
    body: dict[str, object] = {
        "operation_id": op.operation_id,
        "state": state,
        "kind": op.kind,
        "target_id": op.target_id,
        "target_revision": op.target_revision,
        "updated_at": op.updated_at,
    }
    if op.error is not None and state != "applied":
        body["code"] = op.error
    return body


def create_app(
    config: ServerConfig,
    whois: Whois | None,
    *,
    store: MobileStore | None = None,
    archive: ArchiveStore | None = None,
    summary: SummaryState | None = None,
    actions: ActionCourier | None = None,
    clock: Callable[[], datetime] | None = None,
    local_bind: tuple[str, int] | None = None,
) -> FastAPI:
    mobile = config.mobile
    if mobile.bind is None and local_bind is None:
        raise ValueError("mobile.bind is not configured")
    store = store or MobileStore(config.state_root)
    archive = archive or ArchiveStore.from_config(config)
    summary = summary or SummaryState(config.state_root)
    alerts = AlertDelivery(config).history
    pipeline = PipelineReadModel(config.state_root)
    reviews = ReviewService(summary, store)
    captures = CaptureService(
        archive,
        store,
        config.state_root / "mobile" / "staging",
        max_payload_bytes=mobile.max_payload_bytes,
        staging_days=mobile.staging_days,
    )
    read = ReadModel(config, archive, clock)
    results = ResultModel(read)
    request_edits = RequestEditService(config, store)
    edits = EditService(config, store)
    feedback_store = FeedbackStore(config.state_root / "wiki-feedback.sqlite3")
    feedback = FeedbackService(config, store, feedback_store, results)
    feed = FeedProducer(
        store,
        reviews,
        actions,
        read.head,
        action_dispatch=lambda: mobile.transport_for("action") == "http",
        sources=FeedSources(
            review_http=lambda: mobile.transport_for("review") == "http",
            alerts=alerts,
            alerts_http=lambda: mobile.transport_for("alert") == "http",
            pipeline=pipeline,
            pipeline_http=lambda: mobile.transport_for("pipeline") == "http",
            wiki_hints=read.wiki_hints,
        ),
    )

    app = FastAPI(
        title="weaver-mobile", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(
        AuthMiddleware,
        store=store,
        whois=whois,
        allowed_hosts=allowed_hosts(*(local_bind or (mobile.bind or "", mobile.port))),
        local_bind=local_bind,
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
        payload = utc_z(jsonable_encoder(value))
        return JSONResponse(
            payload,
            status_code=status,
            headers={"Cache-Control": "no-store", **(headers or {})},
        )

    # ---- capabilities ----------------------------------------------------------

    @router.get("/capabilities")
    def capabilities(who: Anyone) -> JSONResponse:
        # Results cards and document feedback exist only for a PARA tree.
        dictionary = read.snapshot().dictionary
        has_tree = dictionary is not None and dictionary.has_tree
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
                    **({"alert": 2} if mobile.transport_for("alert") == "http" else {}),
                    **(
                        {"pipeline": 2}
                        if mobile.transport_for("pipeline") == "http"
                        else {}
                    ),
                },
                "transports": {
                    feature: mobile.transport_for(feature)
                    for feature in ("capture", "review", "action", "alert", "pipeline")
                    if feature not in ("alert", "pipeline")
                    or mobile.transport_for(feature) != "files"
                },
                "limits": {
                    "max_payload_bytes": mobile.max_payload_bytes,
                    "max_json_bytes": MAX_JSON_BYTES,
                    "max_feed_items": MAX_FEED_PAGE,
                    "max_feed_bytes": MAX_FEED_BYTES,
                },
                "features": {
                    "capture": True,
                    "review": True,
                    "action": True,
                    "read": True,
                    "dictionary": True,
                    "edits": True,
                    "thread_handling": True,
                    "results": has_tree,
                    "feedback": has_tree,
                    "alerts": mobile.transport_for("alert") == "http",
                    "pipeline": mobile.transport_for("pipeline") == "http",
                },
                "feed_kinds": sorted(feed.served_kinds()),
                "feed_epoch": store.feed_epoch,
                "device": {
                    "device_id": who.device.device_id,
                    "scopes": sorted(who.device.scopes),
                    "executor": who.device.executor,
                },
            }
        )

    @router.post("/diagnostics/crashes")
    async def crash(request: Request, who: Anyone) -> JSONResponse:
        status, body = CrashStore(config.state_root).submit(
            await _body(request, 64 * 1024), who.device.device_id
        )
        return json_response(body, status)

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
            {
                "items": [ReviewEntry(p).summary for p in reviews.open_packages()],
                "next_cursor": None,
            }
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

    # ---- alerts (durable receipt, never local dismiss) --------------------------

    def alert_http() -> None:
        if mobile.transport_for("alert") != "http":
            raise ApiError(409, "transport_files")

    @router.get("/alerts")
    def list_alerts(who: ReviewScope) -> JSONResponse:
        alert_http()
        return json_response(
            {
                "items": alerts.bodies(device_id=who.device.device_id),
                "next_cursor": None,
            }
        )

    @router.get("/alerts/{alert_id}")
    def get_alert(alert_id: str, _who: ReviewScope) -> JSONResponse:
        alert_http()
        try:
            alert = alerts.body(alert_id)
        except AlertBodyExpired:
            raise ApiError(410, "alert_body_expired") from None
        if alert is None:
            raise ApiError(404, "alert_not_found")
        return json_response(
            alert, headers={"ETag": '"' + content_hash(alert.model_dump_json()) + '"'}
        )

    @router.put("/alerts/{alert_id}/ack")
    def ack_alert(alert_id: str, who: ReviewScope) -> JSONResponse:
        alert_http()
        try:
            received = alerts.ack(
                who.device.device_id, alert_id, (clock or (lambda: datetime.now(UTC)))()
            )
        except AlertBodyExpired:
            raise ApiError(410, "alert_body_expired") from None
        if not received:
            raise ApiError(404, "alert_not_found")
        return json_response({"alert_id": alert_id, "state": "received"})

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
        return json_response({"items": actions.dispatchable(), "next_cursor": None})

    @router.get("/actions/{action_id}")
    def get_action(action_id: str, _who: Executor) -> JSONResponse:
        if actions is None or mobile.transport_for("action") != "http":
            raise ApiError(409, "transport_files")
        action = actions.dispatchable_one(action_id)
        if action is None:
            raise ApiError(404, "action_not_found")
        return json_response(action)

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
        kinds: str | None = None,
    ) -> JSONResponse:
        feed.reconcile()
        epoch = store.feed_epoch
        # A phone that does not name its kinds is an older build: it never sees a
        # kind added after it shipped. Kinds a feature handed back to files, or a
        # scope the device lacks, are hidden, not deleted (no tombstone).
        asked = (
            LEGACY_KINDS
            if kinds is None
            else frozenset(k for k in kinds.split(",") if k in KIND_SCOPES)
        )
        # The cursor pins the requested kinds: continuing with another set would
        # silently skip the new kinds' older entries, so the server says 410.
        pin = kinds_pin(asked)
        after = 0
        if cursor is not None:
            match = CURSOR.fullmatch(cursor)
            if match is None:
                raise ApiError(422, "invalid_cursor")
            if match.group(1) != epoch or int(match.group(2)) > store.feed_head():
                raise ApiError(410, "cursor_expired")
            # A cursor from before the pin existed is valid for the legacy set only.
            if (match.group(3) or (pin if asked == LEGACY_KINDS else None)) != pin:
                raise ApiError(410, "cursor_expired")
            after = int(match.group(2))
        allowed = {
            kind
            for kind in asked & feed.served_kinds()
            if KIND_SCOPES[kind] in who.device.scopes
        }
        items, more = store.feed_page(after, limit)
        shown: list[dict[str, object]] = []
        size = 0
        last = after
        for item in items:
            if item.kind in allowed:
                entry = {
                    "seq": item.seq,
                    "kind": item.kind,
                    "id": item.resource_id,
                    "revision": item.revision,
                    "op": item.op,
                    "body": None if item.body is None else json.loads(item.body),
                }
                size += len(canonical(entry).encode())
                if shown and size > MAX_FEED_BYTES:
                    # Page break: resume right after the last item that was sent.
                    more = True
                    break
                shown.append(entry)
            last = item.seq
        return json_response(
            {
                "feed_epoch": epoch,
                "items": shown,
                "next_cursor": f"{epoch}.{last}.{pin}",
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
    def read_now(
        _who: ReadScope,
        after_seq: Annotated[int | None, Query(ge=0)] = None,
        fabric_epoch: str | None = None,
    ) -> JSONResponse:
        return json_response(read.now(after_seq=after_seq, fabric_epoch=fabric_epoch))

    @router.get("/read/threads")
    def read_threads(
        _who: ReadScope,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> JSONResponse:
        return json_response(read.threads(cursor, limit))

    @router.get("/read/threads/{thread_id}")
    def read_thread(thread_id: str, _who: ReadScope) -> JSONResponse:
        return json_response(read.thread(thread_id))

    @router.get("/read/tags")
    def read_tags(_who: ReadScope) -> JSONResponse:
        return json_response(
            {"items": [t.model_dump() for t in read.tags()], "next_cursor": None}
        )

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
            {
                "items": [
                    r.model_dump(mode="json")
                    for r in read.timeline(subject, mentions, history)
                ],
                "next_cursor": None,
            }
        )

    @router.get("/read/dictionary")
    def read_dictionary(_who: ReadScope) -> JSONResponse:
        return json_response({"items": read.dictionary(), "next_cursor": None})

    @router.get("/read/proposals")
    def read_proposals(
        _who: ReadScope,
        status: Literal["pending", "accepted", "rejected"] = "pending",
    ) -> JSONResponse:
        return json_response(
            {
                "items": [p.model_dump(mode="json") for p in read.proposals(status)],
                "next_cursor": None,
            }
        )

    def instant(at: str | None) -> datetime | None:
        if at is None:
            return None
        try:
            value = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            raise ApiError(422, "invalid_at") from None
        if value.tzinfo is None:
            raise ApiError(422, "invalid_at")
        return value

    @router.get("/read/cards")
    def read_cards(
        _who: ReadScope,
        window: Literal["today", "around"] = "today",
        at: str | None = None,
    ) -> JSONResponse:
        moment = read.clock() if at is None else instant(at)
        assert moment is not None
        zone = config.time.zone
        return json_response(
            {
                "items": [
                    c.model_dump(mode="json") for c in results.cards(moment, window)
                ],
                "next_cursor": None,
                "today": moment.astimezone(zone).date().isoformat(),
                "timezone": str(zone),
            }
        )

    @router.get("/read/tasks")
    def read_tasks(_who: ReadScope, at: str | None = None) -> JSONResponse:
        return json_response(
            {
                "items": [
                    t.model_dump(mode="json") for t in results.tasks(instant(at))
                ],
                "next_cursor": None,
            }
        )

    @router.get("/read/tree")
    def read_tree(_who: ReadScope, at: str | None = None) -> JSONResponse:
        return json_response(
            {"items": [n.model_dump(mode="json") for n in results.tree(instant(at))]}
        )

    @router.get("/read/references")
    def read_references(
        _who: ReadScope, path: Annotated[str, Query(min_length=1)]
    ) -> JSONResponse:
        return json_response(
            {"items": [r.model_dump(mode="json") for r in results.references(path)]}
        )

    @router.get("/read/documents")
    def read_documents(_who: ReadScope) -> JSONResponse:
        return json_response(
            {"items": [d.model_dump(mode="json") for d in results.documents()]}
        )

    @router.get("/read/documents/{document_id}")
    def read_document(
        document_id: str,
        request: Request,
        _who: ReadScope,
        at: str | None = None,
    ) -> Response:
        view = results.document(document_id, instant(at))
        # Remaining and waiting are live, so the tag covers the whole view
        # rather than only the committed file's sha256.
        tag = '"' + content_hash(view.model_dump_json()) + '"'
        if request.headers.get("if-none-match") == tag:
            return Response(
                status_code=304,
                headers={"ETag": tag, "Cache-Control": "no-store"},
            )
        return json_response(view, headers={"ETag": tag})

    @router.get("/read/documents/{document_id}/feedback")
    def read_feedback(document_id: str, _who: ReadScope) -> JSONResponse:
        results.document_key(document_id)
        return json_response(
            {
                "items": [
                    i.model_dump(mode="json")
                    for i in feedback_store.for_document(document_id)
                ],
                "next_cursor": None,
            }
        )

    @router.get("/read/documents/{document_id}/bullets/{bullet_id}/evidence")
    def read_evidence(
        document_id: str, bullet_id: str, _who: ReadScope
    ) -> JSONResponse:
        return json_response(results.evidence(document_id, bullet_id))

    @router.get("/read/sources")
    def read_sources(
        _who: ReadScope,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        sphere: str | None = None,
        person: str | None = None,
        kind: str | None = None,
    ) -> JSONResponse:
        items, next_cursor = results.sources(cursor, limit, sphere, person, kind)
        return json_response(
            {
                "items": [e.model_dump(mode="json") for e in items],
                "next_cursor": next_cursor,
            }
        )

    def pipeline_http() -> None:
        if mobile.transport_for("pipeline") != "http":
            raise ApiError(409, "transport_files")

    @router.get("/read/pipeline")
    def read_pipeline(
        _who: ReadScope,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> JSONResponse:
        pipeline_http()
        return json_response(pipeline.page(cursor, limit))

    @router.get("/pipeline")
    def legacy_pipeline(
        _who: ReviewScope,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> JSONResponse:
        # APKs before the feed client pull the same list from here (review scope,
        # as their file route was); the body is identical to /read/pipeline.
        pipeline_http()
        return json_response(pipeline.page(cursor, limit))

    @router.get("/read/pipeline/{source_id}")
    def read_pipeline_item(
        source_id: str, request: Request, _who: ReadScope
    ) -> Response:
        pipeline_http()
        entry = pipeline.get(source_id)
        tag = f'"{entry.etag}"'
        if request.headers.get("if-none-match") == tag:
            return Response(
                status_code=304,
                headers={"ETag": tag, "Cache-Control": "no-store"},
            )
        return json_response(entry.payload, headers={"ETag": tag})

    @router.get("/read/wiki/pages")
    def read_wiki_pages(_who: ReadScope) -> JSONResponse:
        return json_response(
            {
                "items": [p.model_dump(mode="json") for p in read.wiki_pages()],
                "next_cursor": None,
            }
        )

    @router.get("/read/wiki/pages/{page_id}")
    def read_wiki_page(page_id: str, request: Request, _who: ReadScope) -> Response:
        page = read.wiki_page(page_id)
        tag = f'"{page.sha256}"'
        if request.headers.get("if-none-match") == tag:
            return Response(
                status_code=304,
                headers={"ETag": tag, "Cache-Control": "no-store"},
            )
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

    @router.post("/edits/requests/complete")
    async def complete_request(request: Request, who: EditScope) -> JSONResponse:
        edit = await parse(request, RequestEdit)
        return edit_response(
            *await _in_thread(
                request_edits.apply, who.device.device_id, "complete", edit
            )
        )

    @router.post("/edits/requests/deadline")
    async def change_deadline(request: Request, who: EditScope) -> JSONResponse:
        edit = await parse(request, DeadlineEdit)
        return edit_response(
            *await _in_thread(
                request_edits.apply, who.device.device_id, "deadline", edit
            )
        )

    @router.post("/edits/requests/undo")
    async def undo_request_edit(request: Request, who: EditScope) -> JSONResponse:
        edit = await parse(request, UndoEdit)
        return edit_response(
            *await _in_thread(request_edits.apply, who.device.device_id, "undo", edit)
        )

    @router.post("/edits/feedback")
    async def submit_feedback(request: Request, who: EditScope) -> JSONResponse:
        edit = await parse(request, FeedbackEdit)
        return edit_response(
            *await _in_thread(feedback.submit, who.device.device_id, edit)
        )

    @router.post("/edits/feedback/undo")
    async def undo_feedback_edit(request: Request, who: EditScope) -> JSONResponse:
        edit = await parse(request, FeedbackUndo)
        return edit_response(
            *await _in_thread(feedback.undo, who.device.device_id, edit)
        )

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
