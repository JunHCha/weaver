"""Deterministic scenarios that produce every contract fixture group."""

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from weaver.action import ActionJob
from weaver.context import ContextDictionary
from weaver.context.proposals import LocationProposals
from weaver.context.tree import TreeNode, node_kind
from weaver.mobile.whois import PeerIdentity
from weaver.wiki import layout
from weaver.wiki.store import WikiStore

from .contract import Recorder
from .helpers import BODY, CAP, CAP2, LOGIN, NODE, Harness, manifest, result, verdict
from .test_edits import seed as seed_edits
from .test_read import seed as seed_read

BASE = "/mobile/v1"
NOW = datetime(2026, 11, 12, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))
HTTP = 'transport = "http"\n'


def auth(headers: dict[str, str], **extra: str) -> dict[str, str]:
    return {**headers, **extra}


def with_tree(h: Harness) -> Harness:
    """A vault tree turns on the results and feedback features."""
    ContextDictionary(h.config.state_root).sync_tree(
        "회사", [TreeNode(n, node_kind(n)) for n in ("회사", "회사/infra")]
    )
    return h


def capabilities(tmp: Path) -> Recorder:
    h = with_tree(
        Harness(tmp, '[mobile.features]\ncapture = "http"\n', clock=lambda: NOW)
    )
    headers = h.pair("read", "capture", "review", "edit")
    r = Recorder(
        h,
        "capabilities",
        "Server identity, transports per feature, limits and feature flags.",
    )
    r.call(
        "capabilities",
        "GET",
        f"{BASE}/capabilities",
        headers=headers,
        note="transports is the single source of truth for which path each feature uses; "
        "features.alerts/pipeline=false means keep reading those folders through files.",
    )
    return r


def errors(tmp: Path) -> Recorder:
    h = Harness(tmp, clock=lambda: NOW)
    plain = h.pair("read", label="reader")
    r = Recorder(
        h,
        "errors",
        "Authentication and common refusals. Body is always {code, retryable, ...}.",
    )
    h.whois.unavailable = True
    r.call(
        "whois_unavailable",
        "GET",
        f"{BASE}/capabilities",
        headers=plain,
        note="Transient: tailscaled could not answer. Retry with backoff; not a re-pairing case.",
    )
    h.whois.unavailable = False
    h.whois.identity = None
    r.call("peer_unverified", "GET", f"{BASE}/capabilities", headers=plain)
    h.whois.identity = PeerIdentity("nOTHER0001", LOGIN)
    r.call(
        "node_mismatch",
        "GET",
        f"{BASE}/capabilities",
        headers=plain,
        note="With 401 invalid_token/token_revoked this means: pair again.",
    )
    h.whois.identity = PeerIdentity(NODE, LOGIN)
    r.call(
        "invalid_token",
        "GET",
        f"{BASE}/capabilities",
        headers={"Authorization": "Bearer wvr_wrong"},
    )
    r.call("missing_token", "GET", f"{BASE}/capabilities")
    r.call(
        "scope_denied",
        "GET",
        f"{BASE}/reviews",
        headers=plain,
        note="Only that feature is blocked; the rest keeps working.",
    )
    r.call(
        "invalid_host",
        "GET",
        f"{BASE}/capabilities",
        headers=auth(plain, Host="evil.example"),
    )
    r.call(
        "origin_not_allowed",
        "GET",
        f"{BASE}/capabilities",
        headers=auth(plain, Origin="https://127.0.0.1:8787"),
    )
    r.call("not_found", "GET", f"{BASE}/nope", headers=plain)
    r.call(
        "invalid_request",
        "GET",
        f"{BASE}/read/threads",
        headers=plain,
        params={"limit": 501},
    )
    r.call(
        "not_executor",
        "GET",
        f"{BASE}/actions",
        headers=h.pair("read", "action", label="tablet"),
    )
    revoked = h.pair("read", label="gone")
    h.store.revoke(h.device.device_id)
    r.call("token_revoked", "GET", f"{BASE}/capabilities", headers=revoked)
    return r


def captures(tmp: Path) -> Recorder:
    h = Harness(tmp, clock=lambda: NOW)
    headers = h.pair("capture")
    r = Recorder(
        h,
        "captures",
        "payload, then manifest, then Receipt. A staged payload is not a Receipt.",
    )
    sha = hashlib.sha256(BODY).hexdigest()
    put = {"X-Content-SHA256": sha, "Content-Type": "application/octet-stream"}
    r.call(
        "receipt_before_upload",
        "GET",
        f"{BASE}/captures/{CAP}/receipt",
        headers=headers,
    )
    r.call(
        "manifest_before_payload",
        "PUT",
        f"{BASE}/captures/{CAP}/manifest",
        headers=headers,
        content=manifest().model_dump_json(),
        note="Retryable: upload the payload first.",
    )
    r.call(
        "payload_staged",
        "PUT",
        f"{BASE}/captures/{CAP}/payload",
        headers=auth(headers, **put),
        content=BODY,
    )
    r.call(
        "payload_repeat",
        "PUT",
        f"{BASE}/captures/{CAP}/payload",
        headers=auth(headers, **put),
        content=BODY,
    )
    r.call(
        "payload_wrong_sha",
        "PUT",
        f"{BASE}/captures/{CAP2}/payload",
        headers=auth(headers, **{"X-Content-SHA256": "0" * 64}),
        content=BODY,
    )
    r.call(
        "call_occurred_at_required",
        "PUT",
        f"{BASE}/captures/{CAP}/manifest",
        headers=headers,
        content=manifest(channel="call", occurred_at=None).model_dump_json(),
        note="Call occurrence must come from the canonical filename; blocked without retry.",
    )
    r.call(
        "manifest_stored",
        "PUT",
        f"{BASE}/captures/{CAP}/manifest",
        headers=headers,
        content=manifest().model_dump_json(),
        note="201 Receipt: the capture is durably archived.",
    )
    r.call(
        "manifest_replay",
        "PUT",
        f"{BASE}/captures/{CAP}/manifest",
        headers=headers,
        content=manifest().model_dump_json(),
        note="Lost response: 200 with the original Receipt.",
    )
    r.call("receipt", "GET", f"{BASE}/captures/{CAP}/receipt", headers=headers)
    r.call(
        "payload_after_store",
        "PUT",
        f"{BASE}/captures/{CAP}/payload",
        headers=auth(headers, **put),
        content=BODY,
    )
    other = b"other bytes"
    r.call(
        "payload_other_content",
        "PUT",
        f"{BASE}/captures/{CAP}/payload",
        headers=auth(
            headers, **{"X-Content-SHA256": hashlib.sha256(other).hexdigest()}
        ),
        content=other,
    )
    r.call(
        "manifest_conflict",
        "PUT",
        f"{BASE}/captures/{CAP}/manifest",
        headers=headers,
        content=manifest(captured_at="2026-11-08T00:00:00Z").model_dump_json(),
    )
    return r


def reviews(tmp: Path) -> Recorder:
    h = Harness(tmp, clock=lambda: NOW)
    headers = h.pair("review")
    h.add_package("pkg_a")
    r = Recorder(
        h,
        "reviews",
        "Open review packages, If-Match verdicts, and the operation lifecycle.",
    )
    listing = r.call("list", "GET", f"{BASE}/reviews", headers=headers)
    etag = listing.json()["items"][0]["etag"]
    r.call("get_revision", "GET", f"{BASE}/reviews/pkg_a/revisions/1", headers=headers)
    url = f"{BASE}/reviews/pkg_a/revisions/1/verdict"
    r.call("verdict_without_if_match", "PUT", url, headers=headers, json_body=verdict())
    r.call(
        "verdict_wrong_etag",
        "PUT",
        url,
        headers=auth(headers, **{"If-Match": '"nope"'}),
        json_body=verdict(),
    )
    first = r.call(
        "verdict_accepted",
        "PUT",
        url,
        headers=auth(headers, **{"If-Match": etag}),
        json_body=verdict(),
        note="202 = durably accepted, not applied. Poll the operation.",
    )
    r.call(
        "verdict_resent",
        "PUT",
        url,
        headers=auth(headers, **{"If-Match": etag}),
        json_body=verdict(),
        note="Same key and content returns the same operation.",
    )
    r.call(
        "verdict_conflict",
        "PUT",
        url,
        headers=auth(headers, **{"If-Match": etag}),
        json_body=verdict(action="reject"),
    )
    op = f"{BASE}/operations/{first.json()['operation_id']}"
    r.call("operation_queued", "GET", op, headers=headers)
    h.decisions.fail = RuntimeError("x")
    h.tick()
    r.call(
        "operation_waiting",
        "GET",
        op,
        headers=headers,
        note="state stays queued while code says why it waits (apply_waiting, apply_failed, ...).",
    )
    h.decisions.fail = None
    h.tick(10)
    r.call("operation_applied", "GET", op, headers=headers)
    h.add_package("pkg_b")
    stale_tag = h.client.get(
        f"{BASE}/reviews/pkg_b/revisions/1", headers=headers
    ).headers["etag"]
    h.add_package("pkg_b", 2)
    r.call(
        "verdict_stale_revision",
        "PUT",
        f"{BASE}/reviews/pkg_b/revisions/1/verdict",
        headers=auth(headers, **{"If-Match": stale_tag}),
        json_body=verdict("pkg_b"),
        note="Keep the draft; fetch the new revision and review again.",
    )
    r.call(
        "package_not_found", "GET", f"{BASE}/reviews/pkg_a/revisions/9", headers=headers
    )
    r.call(
        "operation_not_found", "GET", f"{BASE}/operations/op_missing", headers=headers
    )
    return r


def actions(tmp: Path) -> Recorder:
    h = Harness(tmp, HTTP, clock=lambda: NOW)
    job = ActionJob(h.fabric, h.outbox, h.actions)
    from .helpers import schedule

    h.fabric.append([schedule("s1")])
    job.run()
    fold = h.pair("read", "action", executor=True)
    r = Recorder(
        h, "actions", "Action dispatch to the single executor phone and ActionResult."
    )
    listing = r.call("list", "GET", f"{BASE}/actions", headers=fold)
    action_id = listing.json()["items"][0]["action_id"]
    r.call("get_one", "GET", f"{BASE}/actions/{action_id}", headers=fold)
    r.call("get_unknown", "GET", f"{BASE}/actions/act_{'0' * 32}", headers=fold)
    url = f"{BASE}/actions/{action_id}/result"
    r.call("result_invalid", "PUT", url, headers=fold, json_body={"x": 1})
    accepted = r.call(
        "result_accepted",
        "PUT",
        url,
        headers=fold,
        json_body=result(action_id),
        note="202; ActionResult is final once the operation is applied.",
    )
    r.call("result_resent", "PUT", url, headers=fold, json_body=result(action_id))
    r.call(
        "result_conflict",
        "PUT",
        url,
        headers=fold,
        json_body=result(action_id, "failed"),
    )
    h.applier.run()
    r.call(
        "operation_applied",
        "GET",
        f"{BASE}/operations/{accepted.json()['operation_id']}",
        headers=fold,
    )
    r.call("list_after_finish", "GET", f"{BASE}/actions", headers=fold)
    unknown = "act_" + "2" * 32
    r.call(
        "result_unknown_action",
        "PUT",
        f"{BASE}/actions/{unknown}/result",
        headers=fold,
        json_body=result(unknown),
    )
    return r


def actions_files(tmp: Path) -> Recorder:
    h = Harness(tmp, clock=lambda: NOW)
    fold = h.pair("read", "action", executor=True)
    r = Recorder(
        h,
        "actions_files",
        "While actions use the files transport, HTTP dispatch is closed.",
    )
    r.call(
        "transport_files",
        "GET",
        f"{BASE}/actions",
        headers=fold,
        note="Follow capabilities.transports.action; do not execute from files and HTTP at once.",
    )
    return r


def changes(tmp: Path) -> Recorder:
    h = Harness(tmp, '[mobile.features]\nreview = "http"\n', clock=lambda: NOW)
    headers = h.pair("read", "review")
    r = Recorder(
        h, "changes", "Change feed (epoch.seq cursor, compacted) and device checkpoint."
    )
    first = r.call(
        "snapshot",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        note="Without a cursor the feed is a complete snapshot. The feed is the one list "
        "whose next_cursor is never null: keep it as the resume point.",
    )
    h.add_package("pkg_a")
    cursor = first.json()["next_cursor"]
    second = r.call(
        "incremental",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"cursor": cursor, "limit": 100},
    )
    r.call(
        "up_to_date",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"cursor": second.json()["next_cursor"]},
    )
    r.call(
        "checkpoint",
        "PUT",
        f"{BASE}/devices/self/checkpoint",
        headers=headers,
        json_body={"cursor": second.json()["next_cursor"]},
    )
    etag = h.client.get(f"{BASE}/reviews/pkg_a/revisions/1", headers=headers).headers[
        "etag"
    ]
    h.client.put(
        f"{BASE}/reviews/pkg_a/revisions/1/verdict",
        json=verdict(),
        headers=auth(headers, **{"If-Match": etag}),
    )
    h.applier.run()
    r.call(
        "tombstone",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"cursor": second.json()["next_cursor"]},
    )
    epoch = cursor.split(".")[0]
    r.call(
        "cursor_expired",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"cursor": f"{epoch}.9999"},
        note="Epoch changed or cursor ahead of the server: start over from no cursor.",
    )
    r.call(
        "invalid_cursor",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"cursor": "garbage"},
    )
    return r


def read(tmp: Path) -> Recorder:
    h = Harness(tmp, clock=lambda: NOW)
    seed_read(h)
    headers = h.pair("read", "capture")
    # Two archived sources, so Thread.source shows a call partner and "메모".
    from .helpers import put_manifest, thread, upload

    labels = {}
    for cap, body, kwargs in (
        (
            CAP,
            BODY,
            {
                "channel": "call",
                "occurred_at": "2026-11-07T12:00:00+09:00",
                "hints": {"counterpart": "이서준"},
            },
        ),
        (CAP2, b"memo", {"channel": "voice_memo", "hints": {"counterpart": None}}),
    ):
        upload(h, headers, cap, body)
        labels[cap] = put_manifest(h, headers, manifest(cap, body, **kwargs)).json()[
            "source_id"
        ]
    h.fabric.append(
        [
            thread(
                "x1",
                content="통화 기록",
                counterpart="이서준",
                tags=["네트워크"],
                source_id=labels[CAP],
            ),
            thread(
                "x2", content="음성 메모", tags=["네트워크"], source_id=labels[CAP2]
            ),
        ]
    )
    r = Recorder(
        h,
        "read",
        "Read model: 지금, Thread pages, timelines, tags, states and dictionary.",
    )
    r.call("now", "GET", f"{BASE}/read/now", headers=headers)
    page = r.call(
        "threads_first_page",
        "GET",
        f"{BASE}/read/threads",
        headers=headers,
        params={"limit": 4},
        note="Lists are {items, next_cursor}; next_cursor is null on the last page.",
    )
    middle = r.call(
        "threads_next_page",
        "GET",
        f"{BASE}/read/threads",
        headers=headers,
        params={"limit": 4, "cursor": page.json()["next_cursor"]},
    )
    r.call(
        "threads_last_page",
        "GET",
        f"{BASE}/read/threads",
        headers=headers,
        params={"limit": 4, "cursor": middle.json()["next_cursor"]},
        note="One chain: each next_cursor is the cursor of the following request.",
    )
    r.call(
        "threads_invalid_cursor",
        "GET",
        f"{BASE}/read/threads",
        headers=headers,
        params={"cursor": "zz"},
    )
    r.call("thread", "GET", f"{BASE}/read/threads/t1", headers=headers)
    r.call("thread_unknown", "GET", f"{BASE}/read/threads/nope", headers=headers)
    r.call("tags", "GET", f"{BASE}/read/tags", headers=headers)
    r.call(
        "states",
        "GET",
        f"{BASE}/read/states",
        headers=headers,
        params={"tag": "네트워크"},
    )
    r.call(
        "timeline_person",
        "GET",
        f"{BASE}/read/timeline",
        headers=headers,
        params={"subject": "이서준", "mentions": "true"},
    )
    r.call(
        "timeline_area_with_history",
        "GET",
        f"{BASE}/read/timeline",
        headers=headers,
        params={"subject": "네트워크", "history": "true"},
    )
    r.call("dictionary", "GET", f"{BASE}/read/dictionary", headers=headers)
    proposals = LocationProposals(h.config.state_root)
    proposals.propose(
        kind="location",
        reason="unknown_path",
        sphere="회사",
        path="회사/infra/storage/nas",
        parent="회사/infra",
        fallback="회사/미분류",
        thread_ids=["x1"],
        source_ids=[labels[CAP]],
        now=NOW,
    )
    r.call(
        "proposals",
        "GET",
        f"{BASE}/read/proposals",
        headers=headers,
        params={"status": "pending"},
        note="Location proposals wait for a person; the default status is pending.",
    )
    r.call(
        "proposals_rejected",
        "GET",
        f"{BASE}/read/proposals",
        headers=headers,
        params={"status": "rejected"},
    )
    return r


def wiki(tmp: Path) -> Recorder:
    h = Harness(tmp, clock=lambda: NOW)
    headers = h.pair("read")
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    store.write(
        "pages/방화벽 교체.md",
        {
            "kind": "project",
            "area": "네트워크",
            "status": "진행 중",
            "deadline": "2026-11-30",
            "assignees": ["나"],
        },
        "노후 장비를 교체한다.\n\n## 진행\n- 룰 3건",
        None,
    )
    store.write("pages/네트워크.md", {"kind": "area"}, "사내망 영역", None)
    r = Recorder(
        h, "wiki", "Wiki pages: the list has no body; open a page to get and cache it."
    )
    listing = r.call("list", "GET", f"{BASE}/read/wiki/pages", headers=headers)
    item = listing.json()["items"][1]
    detail = r.call(
        "page", "GET", f"{BASE}/read/wiki/pages/{item['id']}", headers=headers
    )
    r.call(
        "page_not_modified",
        "GET",
        f"{BASE}/read/wiki/pages/{item['id']}",
        headers=auth(headers, **{"If-None-Match": detail.headers["etag"]}),
    )
    r.call("page_not_found", "GET", f"{BASE}/read/wiki/pages/missing", headers=headers)
    return r


def results(tmp: Path) -> Recorder:
    """Result read models over an invented tree, one compiled document and one Digest."""
    from weaver.context import ContextDictionary
    from weaver.context.tree import TreeNode, node_kind

    from .helpers import thread

    network, storage = "회사/infra/network", "회사/infra/storage"
    h = Harness(tmp, clock=lambda: NOW)
    ContextDictionary(h.config.state_root).sync_tree(
        "회사",
        [
            TreeNode(n, node_kind(n))
            for n in ("회사", "회사/infra", network, storage, "회사/미분류")
        ],
    )
    headers = h.pair("read", "edit")
    came = datetime(2026, 11, 12, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    h.fabric.append(
        [
            thread(
                "card_request",
                kind="요청",
                direction="받음",
                content="견적 회신",
                counterpart="김철수",
                tags=[network],
                field="deadline",
                value="2026-11-14",
                occurred_at=came,
                recorded_at=came,
            ),
            thread(
                "card_note",
                content="장비 목록 수령",
                counterpart="이영희",
                tags=[storage],
                occurred_at=came,
                recorded_at=came,
            ),
            thread(
                "card_visit",
                kind="일정",
                content="현장 점검",
                tags=[storage],
                field="schedule",
                value={
                    "start": "2026-11-13T15:00:00+09:00",
                    "end": None,
                    "timezone": "Asia/Seoul",
                },
                occurred_at=datetime(2026, 11, 10, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
                recorded_at=datetime(2026, 11, 10, 9, 0, tzinfo=ZoneInfo("Asia/Seoul")),
            ),
        ]
    )
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    zone = ZoneInfo("Asia/Seoul")
    path = f"pages/{network}.md"
    store.write(
        path,
        {},
        layout.render_document(
            "네트워크",
            came.date(),
            [layout.Bullet("b1", "장비 2종 견적 도착", ("card_request",))],
            layout.Remaining(mine=[], waiting=[]),
            [],
            [],
            zone,
        ),
        None,
        readonly=True,
    )
    store.write_metadata(
        path,
        {
            "page_key": network,
            "title": "네트워크",
            "kind": "area",
            "as_of": came.date().isoformat(),
            "source_ids": [],
            "bullets": [
                {
                    "bullet_id": "b1",
                    "section": "current",
                    "thread_ids": ["card_request"],
                }
            ],
            "cautions": [],
        },
    )
    digest = "Digest/2026/11/2026-11-12.md"
    store.write(
        digest,
        {},
        "# 2026-11-12 (목)\n\n## 들어온 일\n\n"
        + layout.task_line(
            "견적 회신",
            layout.block_id("card_request"),
            deadline=date(2026, 11, 14),
            done=None,
        )
        + "\n\n## 요약\n\n- 네트워크 견적 도착 [[summaries/견적 src_x|견적]]\n",
        None,
        readonly=True,
    )
    document_id = store.page_id(path)
    r = Recorder(
        h,
        "results",
        "Result read models (features.results): cards, my open tasks, tree, documents, evidence and the source log.",
    )
    r.call(
        "cards_today",
        "GET",
        f"{BASE}/read/cards",
        headers=headers,
        params={"window": "today"},
        note="Cards of facts that came in today; link opens a document bullet, else the day's Digest.",
    )
    r.call(
        "cards_around",
        "GET",
        f"{BASE}/read/cards",
        headers=headers,
        params={"window": "around"},
        note="Cards by deadline or schedule day within 7 days of today, today excluded, nearest first.",
    )
    r.call("tasks", "GET", f"{BASE}/read/tasks", headers=headers)
    r.call(
        "cards_invalid_at",
        "GET",
        f"{BASE}/read/cards",
        headers=headers,
        params={"at": "2026-11-12"},
        note="at must be an ISO instant with an explicit offset.",
    )
    r.call("tree", "GET", f"{BASE}/read/tree", headers=headers)
    r.call(
        "references",
        "GET",
        f"{BASE}/read/references",
        headers=headers,
        params={"path": network},
        note="Reference chunks related to a tree path; empty while nothing is indexed.",
    )
    r.call("documents", "GET", f"{BASE}/read/documents", headers=headers)
    detail = r.call(
        "document",
        "GET",
        f"{BASE}/read/documents/{document_id}",
        headers=headers,
        note="remaining and waiting are recomputed from Fabric on every read; "
        "the ETag covers the whole view.",
    )
    r.call(
        "document_not_modified",
        "GET",
        f"{BASE}/read/documents/{document_id}",
        headers=auth(headers, **{"If-None-Match": detail.headers["etag"]}),
    )
    r.call(
        "document_not_found",
        "GET",
        f"{BASE}/read/documents/missing",
        headers=headers,
    )
    r.call(
        "evidence",
        "GET",
        f"{BASE}/read/documents/{document_id}/bullets/b1/evidence",
        headers=headers,
        note="A bullet's Threads in citation order and each source's whole text "
        "(cut at 20000 characters; truncated says so).",
    )
    r.call(
        "evidence_bullet_not_found",
        "GET",
        f"{BASE}/read/documents/{document_id}/bullets/b9/evidence",
        headers=headers,
    )
    r.call(
        "sources",
        "GET",
        f"{BASE}/read/sources",
        headers=headers,
        params={"limit": 2},
        note="One row per source, newest first; cursor is o<offset>.",
    )
    r.call(
        "sources_filtered",
        "GET",
        f"{BASE}/read/sources",
        headers=headers,
        params={"person": "김철수", "kind": "요청"},
    )
    r.call(
        "sources_invalid_cursor",
        "GET",
        f"{BASE}/read/sources",
        headers=headers,
        params={"cursor": "x"},
    )
    note = {
        "client_id": "feedback-0001",
        "document_id": document_id,
        "bullet_id": "b1",
        "text": "견적 회신은 김철수가 아니라 홍길동 담당",
        "chips": ["사실이 틀림"],
    }
    sent = r.call(
        "feedback_submit",
        "POST",
        f"{BASE}/edits/feedback",
        headers=headers,
        json_body=note,
        note="features.feedback. Stored as pending; the server classifies it on its "
        "next wiki run. client_id is the idempotency key.",
    )
    r.call(
        "feedback_submit_replay",
        "POST",
        f"{BASE}/edits/feedback",
        headers=headers,
        json_body=note,
    )
    r.call(
        "feedback_document_not_found",
        "POST",
        f"{BASE}/edits/feedback",
        headers=headers,
        json_body={**note, "client_id": "feedback-0002", "document_id": "missing"},
    )
    r.call(
        "feedback_list",
        "GET",
        f"{BASE}/read/documents/{document_id}/feedback",
        headers=headers,
    )
    undo = {
        "client_id": "feedback-undo-01",
        "feedback_id": sent.json()["result"]["feedback_id"],
    }
    r.call(
        "feedback_undo",
        "POST",
        f"{BASE}/edits/feedback/undo",
        headers=headers,
        json_body=undo,
        note="A pending note is never processed afterwards; an applied one retracts "
        "its Threads and switches its directive off.",
    )
    r.call(
        "feedback_undo_again",
        "POST",
        f"{BASE}/edits/feedback/undo",
        headers=headers,
        json_body={**undo, "client_id": "feedback-undo-02"},
    )
    return r


def read_shapes(tmp: Path) -> Recorder:
    """Synthetic complex values and Unicode; never copied from a user vault."""
    import unicodedata

    from weaver.context import ContextDictionary

    from .helpers import thread

    h = Harness(tmp, clock=lambda: NOW)
    headers = h.pair("read")
    area = unicodedata.normalize("NFD", "테스트 영역")
    dictionary = ContextDictionary(h.config.state_root)
    dictionary.add_entry(area, "area")
    dictionary.add_entry("보관 프로젝트", "project")
    dictionary.set_project_lifecycle("보관 프로젝트", archived=True)
    schedule = {
        "start": "2026-11-12T15:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
    }
    h.fabric.append(
        [
            thread(
                "shape_schedule",
                kind="일정",
                field="schedule",
                value=schedule,
                tags=[area, "보관 프로젝트"],
                content="합성 본문 " * 512,
            ),
            thread(
                "shape_null",
                kind="요청",
                direction="받음",
                content="합성 요청",
                counterpart=None,
                tags=[area],
            ),
            thread(
                "shape_correction",
                kind="태그정정",
                content="합성 정정",
                correction={"target": "shape_null", "add": [area], "remove": []},
            ),
            thread("shape_rejection", kind="거절", content=None),
            thread(
                "shape_execution",
                kind="실행",
                executes="shape_schedule",
                content="합성 실행",
                actor="system",
                value={
                    "action_id": "act_" + "a" * 32,
                    "action": "calendar.register",
                    "outcome": "completed",
                    "external_id": "synthetic:1",
                    "error": None,
                },
            ),
        ]
    )
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    store.write(
        "pages/합성 문서.md",
        {
            "kind": "project",
            "area": area,
            "status": "진행 중",
            "deadline": None,
            "assignees": ["가상 담당자"],
            "schedule": schedule,
            "next": None,
        },
        "합성 Wiki 본문\n" * 512,
        None,
    )
    r = Recorder(
        h,
        "read_shapes",
        "Synthetic nested values, nulls, long text, NFD and history kinds.",
    )
    r.call("threads", "GET", f"{BASE}/read/threads", headers=headers)
    r.call("tags", "GET", f"{BASE}/read/tags", headers=headers)
    r.call("dictionary", "GET", f"{BASE}/read/dictionary", headers=headers)
    listing = r.call("wiki", "GET", f"{BASE}/read/wiki/pages", headers=headers)
    page_id = listing.json()["items"][0]["id"]
    r.call("wiki_body", "GET", f"{BASE}/read/wiki/pages/{page_id}", headers=headers)
    return r


def edits(tmp: Path) -> Recorder:
    h = Harness(tmp, clock=lambda: NOW)
    seed_edits(h)
    headers = h.pair("edit")
    r = Recorder(
        h, "edits", "Tag and dictionary edits. client_id is the idempotency key."
    )
    body = {
        "client_id": "client-edit-0001",
        "thread_ids": ["record"],
        "add": ["Beta"],
        "remove": ["Alpha"],
    }
    r.call("tags", "POST", f"{BASE}/edits/tags", headers=headers, json_body=body)
    r.call(
        "tags_replay",
        "POST",
        f"{BASE}/edits/tags",
        headers=headers,
        json_body=body,
        note="Same client_id and content: the original result, header Idempotent-Replay: true.",
    )
    r.call(
        "tags_client_id_conflict",
        "POST",
        f"{BASE}/edits/tags",
        headers=headers,
        json_body={**body, "add": ["Gamma"]},
    )
    r.call(
        "tags_history_thread",
        "POST",
        f"{BASE}/edits/tags",
        headers=headers,
        json_body={
            "client_id": "client-edit-0002",
            "thread_ids": ["fix"],
            "add": ["Alpha"],
        },
        note="History Threads are read-only; nothing is written.",
    )
    r.call(
        "tags_unknown_thread",
        "POST",
        f"{BASE}/edits/tags",
        headers=headers,
        json_body={
            "client_id": "client-edit-0003",
            "thread_ids": ["nope"],
            "add": ["Alpha"],
        },
    )
    r.call(
        "tags_new_tag_needs_kind",
        "POST",
        f"{BASE}/edits/tags",
        headers=headers,
        json_body={
            "client_id": "client-edit-0004",
            "thread_ids": ["record"],
            "add": ["Delta"],
        },
    )
    r.call(
        "dictionary_rename",
        "POST",
        f"{BASE}/edits/dictionary/rename",
        headers=headers,
        json_body={"client_id": "client-dict-0001", "entry": "Alpha", "name": "Alpha2"},
    )
    r.call(
        "dictionary_kind",
        "POST",
        f"{BASE}/edits/dictionary/kind",
        headers=headers,
        json_body={
            "client_id": "client-dict-0002",
            "entry": "Gamma",
            "kind": "organization",
        },
    )
    r.call(
        "dictionary_alias_add",
        "POST",
        f"{BASE}/edits/dictionary/alias-add",
        headers=headers,
        json_body={"client_id": "client-dict-0003", "entry": "Alpha2", "alias": "AA"},
    )
    r.call(
        "dictionary_alias_remove",
        "POST",
        f"{BASE}/edits/dictionary/alias-remove",
        headers=headers,
        json_body={"client_id": "client-dict-0004", "entry": "Alpha2", "alias": "AA"},
    )
    r.call(
        "dictionary_merge_kind_choice",
        "POST",
        f"{BASE}/edits/dictionary/merge",
        headers=headers,
        json_body={"client_id": "client-dict-0005", "entry": "Gamma", "target": "Beta"},
    )
    r.call(
        "dictionary_merge",
        "POST",
        f"{BASE}/edits/dictionary/merge",
        headers=headers,
        json_body={
            "client_id": "client-dict-0006",
            "entry": "Gamma",
            "target": "Beta",
            "kind": "person",
        },
    )
    r.call(
        "dictionary_entry_not_found",
        "POST",
        f"{BASE}/edits/dictionary/rename",
        headers=headers,
        json_body={"client_id": "client-dict-0007", "entry": "Nobody", "name": "X"},
    )
    r.call(
        "dictionary_unknown_operation",
        "POST",
        f"{BASE}/edits/dictionary/explode",
        headers=headers,
        json_body={"client_id": "client-dict-0008", "entry": "a"},
    )
    return r


def crashes(tmp: Path) -> Recorder:
    from .test_crashes import PATH, REPORT

    h = Harness(tmp)
    headers = h.pair("read")
    r = Recorder(
        h,
        "crashes",
        "Message-free crash diagnostics: authenticated, no scope required, 64 KiB maximum.",
    )
    r.call(
        "stored",
        "POST",
        PATH,
        headers=auth(headers, **{"Content-Type": "application/json"}),
        json_body=REPORT,
    )
    r.call("replay", "POST", PATH, headers=headers, json_body=REPORT)
    r.call(
        "conflict", "POST", PATH, headers=headers, json_body={**REPORT, "app_build": 2}
    )
    r.call(
        "invalid",
        "POST",
        PATH,
        headers=headers,
        json_body={**REPORT, "message": "synthetic private message"},
    )
    return r


def request_handling(tmp: Path) -> Recorder:
    from .helpers import thread

    h = Harness(tmp, clock=lambda: NOW)
    h.fabric.append(
        [
            thread(
                "request-now",
                kind="요청",
                direction="받음",
                field="deadline",
                value="2026-11-01",
            )
        ]
    )
    headers = h.pair("read", "edit")
    r = Recorder(
        h,
        "request_handling",
        "Human completion/deadline/undo with target revisions and client-ID replay.",
    )
    body = {
        "client_id": "request-deadline-01",
        "thread_id": "request-now",
        "expected_revision": 1,
        "content": "기한을 변경했습니다",
        "deadline": "2026-12-01",
    }
    changed = r.call(
        "deadline",
        "POST",
        f"{BASE}/edits/requests/deadline",
        headers=headers,
        json_body=body,
    )
    r.call(
        "deadline_replay",
        "POST",
        f"{BASE}/edits/requests/deadline",
        headers=headers,
        json_body=body,
    )
    revision = changed.json()["result"]["revision"]
    change_id = changed.json()["result"]["edit_thread_id"]
    r.call("amendment", "GET", f"{BASE}/read/threads/{change_id}", headers=headers)
    completed = r.call(
        "complete",
        "POST",
        f"{BASE}/edits/requests/complete",
        headers=headers,
        json_body={
            "client_id": "request-complete-01",
            "thread_id": "request-now",
            "expected_revision": revision,
            "content": "완료했습니다",
        },
    )
    result = completed.json()["result"]
    r.call(
        "undo",
        "POST",
        f"{BASE}/edits/requests/undo",
        headers=headers,
        json_body={
            "client_id": "request-undo-0001",
            "edit_thread_id": result["edit_thread_id"],
            "expected_revision": result["revision"],
        },
    )
    r.call(
        "now_since_seq",
        "GET",
        f"{BASE}/read/now",
        headers=headers,
        params={"after_seq": 0, "fabric_epoch": h.fabric.epoch()},
    )
    return r


def alerts(tmp: Path) -> Recorder:
    from weaver.alert import AlertHistory
    from weaver.schema import Alert

    h = with_tree(
        Harness(tmp, '[mobile.features]\nalert = "http"\n', clock=lambda: NOW)
    )
    headers = h.pair("review")
    other = h.pair("review", label="second phone")
    body = Alert(
        schema_version=2,
        alert_id="alt_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        package_id="pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        thread_id="thr_2",
        message="합성 일정 알림",
        created_at=NOW,
    )
    history = AlertHistory(h.config.state_root / "alerts.sqlite3")
    history.stage(body, "thr_1")
    history.projected(body.alert_id)
    r = Recorder(
        h, "alerts", "Alert v2 bodies and device receipt ack; never local dismiss."
    )
    r.call("enabled_capabilities", "GET", f"{BASE}/capabilities", headers=headers)
    r.call("list", "GET", f"{BASE}/alerts", headers=headers)
    r.call("body", "GET", f"{BASE}/alerts/{body.alert_id}", headers=headers)
    r.call(
        "receipt",
        "PUT",
        f"{BASE}/alerts/{body.alert_id}/ack",
        headers=headers,
        note="Send only after the phone InboxDatabase body/receipt transaction commits; not on dismiss.",
    )
    r.call(
        "receipt_retry", "PUT", f"{BASE}/alerts/{body.alert_id}/ack", headers=headers
    )
    r.call("received_list", "GET", f"{BASE}/alerts", headers=headers)
    r.call("other_device_list", "GET", f"{BASE}/alerts", headers=other)
    r.call(
        "body_after_receipt", "GET", f"{BASE}/alerts/{body.alert_id}", headers=headers
    )
    r.call("unknown", "GET", f"{BASE}/alerts/unknown", headers=headers)
    r.call("unknown_ack", "PUT", f"{BASE}/alerts/unknown/ack", headers=headers)
    history.ack(h.device.device_id, body.alert_id, NOW)
    history.collect(30, [h.device.device_id], NOW + timedelta(days=31))
    r.call("expired_body", "GET", f"{BASE}/alerts/{body.alert_id}", headers=headers)
    r.call("expired_ack", "PUT", f"{BASE}/alerts/{body.alert_id}/ack", headers=headers)
    return r


def changes_kinds(tmp: Path) -> Recorder:
    from weaver.alert import AlertHistory
    from weaver.schema import Alert

    from .helpers import receive_pipeline

    h = Harness(
        tmp,
        '[mobile.features]\nreview = "http"\nalert = "http"\npipeline = "http"\n',
        clock=lambda: NOW,
    )
    headers = h.pair("read", "review")
    body = Alert(
        schema_version=2,
        alert_id="alt_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        package_id="pkg_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        thread_id="thr_2",
        message="합성 일정 알림",
        created_at=NOW,
    )
    history = AlertHistory(h.config.state_root / "alerts.sqlite3")
    history.stage(body, "thr_1")
    history.projected(body.alert_id)
    receive_pipeline(h, at=NOW)
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    store.write("pages/네트워크.md", {"kind": "area"}, "사내망 영역", None)
    r = Recorder(
        h,
        "changes_kinds",
        "Feed kinds added after the first phone build: alert, pipeline, wiki. "
        "A phone must name them with ?kinds=; without it only review/action/fabric arrive.",
    )
    r.call(
        "legacy_snapshot",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        note="An older build sends no kinds and never sees alert/pipeline/wiki, "
        "even when the server routes them over HTTP.",
    )
    snapshot = r.call(
        "negotiated_snapshot",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"kinds": "review,action,alert,pipeline,wiki,fabric"},
        note="Alert/pipeline/wiki entries are hints: fetch the body from /alerts, "
        "/read/pipeline or /read/wiki. alert needs review scope; pipeline, wiki and fabric need read.",
    )
    r.call(
        "unknown_kind_ignored",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"kinds": "fabric,hologram"},
        note="Kinds the server does not know are ignored so a newer phone still works.",
    )
    r.call(
        "kinds_changed_cursor",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={"kinds": "fabric", "cursor": snapshot.json()["next_cursor"]},
        note="The cursor carries a digest of the kinds it was cut for. Continuing it with "
        "another set would skip the new kinds' older entries, so the server answers 410: "
        "drop the cursor and take a new snapshot for the new set.",
    )
    history.ack(h.device.device_id, body.alert_id, NOW)
    history.collect(30, [h.device.device_id], NOW + timedelta(days=31))
    r.call(
        "alert_body_expired",
        "GET",
        f"{BASE}/changes",
        headers=headers,
        params={
            "kinds": "review,action,alert,pipeline,wiki,fabric",
            "cursor": snapshot.json()["next_cursor"],
        },
        note="Expiry of a retained body tombstones the alert hint. Leaving a route back to "
        "files never writes a tombstone: the kind just disappears from the feed.",
    )
    files = with_tree(Harness(tmp / "files", clock=lambda: NOW))
    other = files.pair("read", "review")
    r.h = files
    r.call(
        "review_on_files",
        "GET",
        f"{BASE}/changes",
        headers=other,
        params={"kinds": "review,fabric"},
        note="While review runs over files the review kind leaves the feed (and "
        "capabilities.feed_kinds) without a tombstone; read proposals/ instead. When it "
        "returns to http every current review entry is announced again under new seq values.",
    )
    return r


def pipeline(tmp: Path) -> Recorder:
    from .helpers import receive_pipeline

    h = with_tree(
        Harness(tmp, '[mobile.features]\npipeline = "http"\n', clock=lambda: NOW)
    )
    headers = h.pair("read")
    receive_pipeline(h, at=NOW)
    files = with_tree(Harness(tmp / "files", clock=lambda: NOW))
    r = Recorder(
        h,
        "pipeline",
        "Pipeline map (PipelineItem v2) over HTTP, outside Courier. Default-valued keys are omitted.",
    )
    r.call("enabled_capabilities", "GET", f"{BASE}/capabilities", headers=headers)
    listing = r.call("list", "GET", f"{BASE}/read/pipeline", headers=headers)
    source_id = listing.json()["items"][0]["source_id"]
    detail = r.call("item", "GET", f"{BASE}/read/pipeline/{source_id}", headers=headers)
    r.call(
        "item_not_modified",
        "GET",
        f"{BASE}/read/pipeline/{source_id}",
        headers=auth(headers, **{"If-None-Match": detail.headers["etag"]}),
    )
    r.call("item_not_found", "GET", f"{BASE}/read/pipeline/missing", headers=headers)
    reviewer = h.pair("review", label="older APK")
    r.call(
        "older_apk_list",
        "GET",
        f"{BASE}/pipeline",
        headers=reviewer,
        note="APKs before the feed client (features.pipeline true) pull this path with "
        "review scope. The body is the same list as /read/pipeline; not for new clients.",
    )
    r.call(
        "stale_cursor",
        "GET",
        f"{BASE}/read/pipeline",
        headers=headers,
        params={"cursor": "0" * 64 + ":1"},
        note="The cursor pins the exact list; a changed list answers 410. The client must "
        "restart the whole list without a cursor and replace what it holds with the new list.",
    )
    r.call(
        "invalid_cursor",
        "GET",
        f"{BASE}/read/pipeline",
        headers=headers,
        params={"cursor": "x"},
    )
    other = files.pair("read")
    r.h = files
    r.call(
        "files_route",
        "GET",
        f"{BASE}/read/pipeline",
        headers=other,
        note="While pipeline is on files the HTTP body is closed; keep reading proposals/pipeline.",
    )
    return r


GROUPS = [
    alerts,
    request_handling,
    capabilities,
    errors,
    captures,
    reviews,
    actions,
    actions_files,
    changes,
    changes_kinds,
    pipeline,
    read,
    wiki,
    results,
    edits,
    read_shapes,
    crashes,
]


def pairing(tmp: Path) -> Recorder:
    """The QR payload printed by ``mobile pair --print-json`` (not an HTTP call)."""
    from typer.testing import CliRunner

    from weaver.app import cli

    from .helpers import write_config

    write_config(tmp)
    out = CliRunner().invoke(
        cli,
        [
            "mobile",
            "pair",
            "--config",
            str(tmp / "config.toml"),
            "--label",
            "fold",
            "--node-id",
            NODE,
            "--print-json",
            "--no-qr",
        ],
    )
    payload = json.loads(out.output.strip().splitlines()[-1])
    payload["ca_sha256"] = "0" * 64
    payload["ca_der_b64"] = "<base64 of the CA certificate (DER)>"
    recorder = Recorder(
        None,
        "pairing",
        "QR payload. The phone pins the CA (not the leaf) and checks the host against the URL's IP and the leaf SAN.",
    )
    recorder.cases.append({"name": "qr_payload", "payload": payload})
    return recorder


def all_groups(root: Path) -> dict[str, dict[str, Any]]:
    documents = {}
    for index, build in enumerate([*GROUPS, pairing]):
        directory = root / f"{index:02d}"
        directory.mkdir()
        recorder = build(directory)
        documents[recorder.group] = recorder.document()
    return documents
