"""Deterministic scenarios that produce every contract fixture group."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from weaver.action import ActionJob
from weaver.mobile.whois import PeerIdentity
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


def capabilities(tmp: Path) -> Recorder:
    h = Harness(tmp, '[mobile.features]\ncapture = "http"\n', clock=lambda: NOW)
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
    h = Harness(tmp, clock=lambda: NOW)
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
        (CAP, BODY, {"channel": "call", "hints": {"counterpart": "이서준"}}),
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


GROUPS = [
    capabilities,
    errors,
    captures,
    reviews,
    actions,
    actions_files,
    changes,
    read,
    wiki,
    edits,
    read_shapes,
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
