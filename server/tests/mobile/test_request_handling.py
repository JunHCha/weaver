"""Synthetic end-to-end request handling and interrupted-commit recovery."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from weaver.fabric import FabricStore, ThreadQuery
from weaver.fabric.store import InvalidReference
from weaver.mobile.read import ReadModel
from weaver.web import create_app

from .helpers import Harness, thread

ROOT = "/mobile/v1"


def seed(h):
    return h.fabric.append(
        [
            thread(
                "req",
                kind="요청",
                direction="받음",
                field="deadline",
                value="2026-11-01",
                tags=["work"],
            )
        ]
    )[0]


def rows(h):
    return h.fabric.entries(ThreadQuery(include_executions=True))


def post(h, headers, name="complete", key="complete-0001", **body):
    return h.client.post(
        f"{ROOT}/edits/requests/{name}",
        headers=headers,
        json={"client_id": key, **body},
    )


def test_complete_retry_conflict_and_undo(tmp_path):
    h = Harness(tmp_path)
    request = seed(h)
    auth = h.pair("read", "edit")
    body = {
        "thread_id": "req",
        "expected_revision": request.seq,
        "content": "견적 회신을 완료했습니다",
    }
    first = post(h, auth, **body)
    assert first.status_code == 200, first.text
    result = first.json()["result"]
    decision = rows(h)[-1]
    assert (decision.kind, decision.actor, decision.resolves) == (
        "결정",
        "human",
        "req",
    )
    assert decision.content == body["content"]
    replay = post(h, auth, **body)
    assert (
        replay.json() == first.json() and replay.headers["Idempotent-Replay"] == "true"
    )
    assert (
        post(h, auth, **(body | {"content": "다른 완료"})).json()["code"]
        == "client_id_conflict"
    )
    assert not h.client.get(f"{ROOT}/read/threads/req", headers=auth).json()["open"]
    undo = post(
        h,
        auth,
        "undo",
        "undo-0000001",
        edit_thread_id=result["edit_thread_id"],
        expected_revision=result["revision"],
    )
    assert undo.status_code == 200, undo.text
    assert rows(h)[-1].retracts == decision.thread_id
    assert h.client.get(f"{ROOT}/read/threads/req", headers=auth).json()["open"]


def test_deadline_folds_into_mobile_web_and_state_without_calendar_action(tmp_path):
    h = Harness(tmp_path)
    request = seed(h)
    auth = h.pair("read", "edit")
    changed = post(
        h,
        auth,
        "deadline",
        deadline="2030-12-01",
        thread_id="req",
        expected_revision=request.seq,
        content="마감을 변경했습니다",
    )
    assert changed.status_code == 200, changed.text
    result = changed.json()["result"]
    amended = rows(h)[-1]
    assert amended.amends == "req" and amended.updates is None
    view = h.client.get(f"{ROOT}/read/threads/req", headers=auth).json()
    assert view["value"] == "2026-11-01" and view["effective_deadline"] == "2030-12-01"
    assert view["revision"] == result["revision"]
    now = h.client.get(f"{ROOT}/read/now?after_seq={request.seq}", headers=auth).json()
    assert any(e["thread"]["thread_id"] == "req" for e in now["recent"])
    state = h.fabric.state("work")
    assert state.open_requests[0].value == "2030-12-01"
    web = TestClient(create_app(h.config), base_url="http://127.0.0.1")
    payload = web.get("/api/threads").json()
    assert (
        next(t for t in payload if t["thread_id"] == "req")["effective_deadline"]
        == "2030-12-01"
    )
    assert next(t for t in payload if t["amends"])["presentation"] == "history"
    noop = post(
        h,
        auth,
        "deadline",
        "deadline-noop",
        deadline="2030-12-01",
        thread_id="req",
        expected_revision=view["revision"],
        content="기한 유지",
    )
    assert noop.status_code == 200 and not noop.json()["result"]["changed"]
    undo = post(
        h,
        auth,
        "undo",
        "undo-deadline",
        edit_thread_id=result["edit_thread_id"],
        expected_revision=view["revision"],
    )
    assert undo.status_code == 200
    assert (
        h.client.get(f"{ROOT}/read/threads/req", headers=auth).json()[
            "effective_deadline"
        ]
        == "2026-11-01"
    )
    from weaver.loom.schedules import approved_schedules

    assert approved_schedules(rows(h), rows(h)) == []


def test_concurrent_view_and_rejected_key_replay(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    auth = h.pair("edit")
    valid = {
        "thread_id": "req",
        "expected_revision": 1,
        "content": "기한 변경",
        "deadline": "2026-12-01",
    }
    assert post(h, auth, "deadline", "deadline-0001", **valid).status_code == 200
    stale = post(
        h,
        auth,
        "complete",
        "complete-stale",
        thread_id="req",
        expected_revision=1,
        content="완료",
    )
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert (
        post(
            h,
            auth,
            "complete",
            "complete-stale",
            thread_id="req",
            expected_revision=1,
            content="완료",
        ).json()
        == stale.json()
    )
    assert len(rows(h)) == 2


def test_append_then_journal_failure_replays_original_row(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    seed(h)
    auth = h.pair("read", "edit")
    original = h.store.update_operation

    def interrupt(operation_id, status, **kwargs):
        if status == "applied":
            raise RuntimeError("crash after Fabric commit")
        return original(operation_id, status, **kwargs)

    monkeypatch.setattr(h.store, "update_operation", interrupt)
    body = {"thread_id": "req", "expected_revision": 1, "content": "완료"}
    with pytest.raises(RuntimeError, match="crash after"):
        post(h, auth, **body)
    before = rows(h)
    assert len(before) == 2
    monkeypatch.setattr(h.store, "update_operation", original)
    # Unrelated facts arriving after the crash must not change the first result.
    h.fabric.append([thread("unrelated")])
    again = post(h, auth, **body)
    assert again.status_code == 200, again.text
    assert again.json()["result"]["edit_thread_id"] == before[-1].thread_id
    assert again.json()["result"]["revision"] == before[-1].seq
    assert len(rows(h)) == 3


@pytest.mark.parametrize(
    ("name", "extra", "code"),
    [
        ("complete", {}, "request_required"),
        ("deadline", {"deadline": "2026-02-30"}, "request_required"),
    ],
)
def test_invalid_targets_do_not_append(tmp_path, name, extra, code):
    h = Harness(tmp_path)
    h.fabric.append([thread("record")])
    auth = h.pair("edit")
    response = post(
        h, auth, name, thread_id="record", expected_revision=1, content="완료", **extra
    )
    assert response.status_code == 422 and response.json()["code"] == code
    assert len(rows(h)) == 1


def test_scope_dates_and_already_resolved(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    readonly = h.pair("read")
    assert (
        post(
            h, readonly, thread_id="req", expected_revision=1, content="완료"
        ).status_code
        == 403
    )
    auth = h.pair("edit")
    assert (
        post(
            h,
            auth,
            "deadline",
            "invalid-date",
            deadline="2026-02-30",
            thread_id="req",
            expected_revision=1,
            content="기한 변경",
        ).json()["code"]
        == "invalid_deadline"
    )
    result = post(h, auth, thread_id="req", expected_revision=1, content="완료").json()[
        "result"
    ]
    again = post(
        h,
        auth,
        key="complete-0002",
        thread_id="req",
        expected_revision=result["revision"],
        content="완료",
    )
    assert (
        again.status_code == 409 and again.json()["code"] == "request_already_resolved"
    )


def test_seq_recent_server_zone_and_epoch(tmp_path):
    h = Harness(tmp_path)
    h.fabric.append(
        [
            thread(
                "old",
                occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
                recorded_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        ]
    )
    read = ReadModel(h.config)
    legacy = read.now(at=datetime(2026, 11, 19, tzinfo=UTC))
    assert legacy.recent == []
    seq = read.now(at=datetime(2026, 11, 19, tzinfo=UTC), after_seq=0)
    assert [x.thread.thread_id for x in seq.recent] == ["old"]
    assert seq.timezone == str(h.config.time.zone) and seq.head == 1
    assert seq.fabric_epoch == FabricStore.from_config(h.config).epoch()
    assert read.now(after_seq=1).recent == []
    auth = h.pair("read")
    assert h.client.get(f"{ROOT}/read/now?after_seq=2", headers=auth).status_code == 409
    assert (
        h.client.get(f"{ROOT}/read/now?after_seq=-1", headers=auth).status_code == 422
    )
    prior = seq.fabric_epoch
    h.fabric.path.unlink()
    assert FabricStore.from_config(h.config).epoch() != prior


def test_amends_validation_and_occurrence_order(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    draft = thread(
        "change",
        kind="결정",
        actor="human",
        amends="req",
        field="deadline",
        value="2026-12-10",
    )
    with pytest.raises(ValidationError):
        thread("bad", kind="기록", amends="req", field="deadline", value="2026-12-10")
    with pytest.raises(InvalidReference):
        h.fabric.append([draft.model_copy(update={"amends": "missing"})])
    later = draft.model_copy(
        update={
            "thread_id": "later",
            "occurred_at": datetime(2030, 1, 1, tzinfo=UTC),
            "value": "2030-12-20",
        }
    )
    h.fabric.append([later, draft])
    # Appended last does not mean occurred last.
    assert ReadModel(h.config).thread("req").effective_deadline == "2030-12-20"


def test_undo_one_resolve_preserves_another_and_new_deadline_blocks_old_undo(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    auth = h.pair("read", "edit")
    first = post(h, auth, thread_id="req", expected_revision=1, content="완료").json()[
        "result"
    ]
    other = h.fabric.append(
        [thread("another-resolution", kind="결정", resolves="req")]
    )[0]
    undone = post(
        h,
        auth,
        "undo",
        "undo-one-resolve",
        edit_thread_id=first["edit_thread_id"],
        expected_revision=other.seq,
    )
    assert undone.status_code == 200
    assert not h.client.get(f"{ROOT}/read/threads/req", headers=auth).json()["open"]
    h.fabric.append([thread("undo-other", kind="결정", retracts="another-resolution")])
    current = ReadModel(h.config).thread("req")
    one = post(
        h,
        auth,
        "deadline",
        "deadline-first",
        thread_id="req",
        expected_revision=current.revision,
        deadline="2027-01-01",
        content="첫 변경",
    ).json()["result"]
    two = post(
        h,
        auth,
        "deadline",
        "deadline-second",
        thread_id="req",
        expected_revision=one["revision"],
        deadline="2027-02-01",
        content="다음 변경",
    ).json()["result"]
    response = post(
        h,
        auth,
        "undo",
        "undo-old-change",
        edit_thread_id=one["edit_thread_id"],
        expected_revision=two["revision"],
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "newer_deadline_change"
    )


def test_tag_correction_does_not_conflict_but_retracted_request_does(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    auth = h.pair("read", "edit")
    h.fabric.append(
        [
            thread(
                "tags",
                kind="태그정정",
                correction={"target": "req", "add": ["new"], "remove": []},
            )
        ]
    )
    assert ReadModel(h.config).thread("req").revision == 1
    response = post(h, auth, thread_id="req", expected_revision=1, content="완료")
    assert response.status_code == 200
    assert rows(h)[-1].tags == ["work", "new"]
    h.fabric.append([thread("retraction", kind="결정", retracts="req")])
    target = ReadModel(h.config).thread("req")
    response = post(
        h,
        auth,
        "deadline",
        "change-retracted",
        thread_id="req",
        expected_revision=target.revision,
        deadline="2027-01-01",
        content="기한 변경",
    )
    assert (
        response.status_code == 409 and response.json()["code"] == "request_retracted"
    )
