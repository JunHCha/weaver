"""Tag and dictionary edits: web rules, client-ID idempotency, lost responses."""

from weaver.context import ContextDictionary
from weaver.fabric import ThreadQuery
from weaver.mobile.edits import EditService, MobileTagEdit

from .helpers import Harness, thread

TAGS = "/mobile/v1/edits/tags"
DICT = "/mobile/v1/edits/dictionary"


def seed(h):
    dictionary = ContextDictionary(h.config.state_root)
    dictionary.add_entry("Alpha", "project", ("A",))
    dictionary.add_entry("Beta", "person")
    dictionary.add_entry("Gamma", "person")
    h.fabric.append(
        [
            thread("record", tags=["Alpha", "old"]),
            thread("second", tags=["Alpha"]),
            thread(
                "fix",
                kind="태그정정",
                content="웹에서 태그 정정",
                tags=[],
                correction={"target": "record", "add": [], "remove": ["old"]},
            ),
        ]
    )


def corrections(h):
    return [
        t
        for t in h.fabric.entries(ThreadQuery())
        if t.kind == "태그정정" and t.thread_id != "fix"
    ]


def edit(h, headers, client_id="client-edit-0001", **body):
    return h.client.post(
        TAGS,
        json={"client_id": client_id, "thread_ids": ["record"], **body},
        headers=headers,
    )


def test_single_and_batch_tag_corrections_reuse_the_web_rules(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")
    one = edit(h, headers, add=["Beta"], remove=["Alpha"])
    assert one.status_code == 200, one.text
    body = one.json()
    assert body["state"] == "applied" and body["result"]["changed"] == 1
    (correction,) = corrections(h)
    assert correction.correction.target == "record"
    assert (correction.correction.add, correction.correction.remove) == (
        ["Beta"],
        ["Alpha"],
    )
    assert (
        correction.actor == "human"
        and correction.thread_id == body["result"]["correction_ids"][0]
    )
    batch = h.client.post(
        TAGS,
        json={
            "client_id": "client-batch-001",
            "thread_ids": ["record", "second"],
            "add": ["Gamma"],
        },
        headers=headers,
    )
    assert batch.json()["result"]["changed"] == 2 and len(corrections(h)) == 3
    unchanged = h.client.post(
        TAGS,
        json={
            "client_id": "client-batch-002",
            "thread_ids": ["record"],
            "add": ["Gamma"],
        },
        headers=headers,
    )
    assert (
        unchanged.json()["result"]["changed"] == 0
        and unchanged.json()["result"]["skipped"] == 1
    )


def test_new_tags_need_a_kind_and_are_registered_as_human_entries(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")
    missing = edit(h, headers, add=["Delta"])
    assert (missing.status_code, missing.json()["code"]) == (422, "kind_required")
    ok = edit(h, headers, "client-edit-0002", add=["Delta"], kinds={"Delta": "topic"})
    assert ok.status_code == 200
    entry = (
        ContextDictionary.open_readonly(h.config.state_root / "context.sqlite3")
        .snapshot()
        .resolve("Delta")
    )
    assert entry.kind == "topic" and entry.human_edited


def test_history_threads_cannot_be_edited_and_nothing_is_written(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")
    before = len(h.fabric.entries(ThreadQuery(include_executions=True)))
    refused = h.client.post(
        TAGS,
        json={"client_id": "client-hist-001", "thread_ids": ["fix"], "add": ["Alpha"]},
        headers=headers,
    )
    assert (refused.status_code, refused.json()) == (
        422,
        {"code": "history_thread_readonly", "retryable": False},
    )
    # A batch with one history row is rejected as a whole.
    mixed = h.client.post(
        TAGS,
        json={
            "client_id": "client-hist-002",
            "thread_ids": ["record", "fix"],
            "add": ["Beta"],
        },
        headers=headers,
    )
    assert mixed.json()["code"] == "history_thread_readonly"
    assert len(h.fabric.entries(ThreadQuery(include_executions=True))) == before
    # Retrying the refused request returns the same refusal.
    again = h.client.post(
        TAGS,
        json={"client_id": "client-hist-001", "thread_ids": ["fix"], "add": ["Alpha"]},
        headers=headers,
    )
    assert again.json()["code"] == "history_thread_readonly"
    missing = h.client.post(
        TAGS,
        json={"client_id": "client-hist-003", "thread_ids": ["nope"], "add": ["Alpha"]},
        headers=headers,
    )
    assert (missing.status_code, missing.json()["code"]) == (404, "thread_not_found")
    conflict = edit(h, headers, "client-hist-004", add=["Beta"], remove=["Beta"])
    assert conflict.json()["code"] == "tag_add_remove_conflict"


def test_retry_with_the_same_client_id_returns_the_original_result(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")
    first = edit(h, headers, add=["Beta"])
    again = edit(h, headers, add=["Beta"])  # the 200 was lost on the way back
    assert again.json() == first.json()
    assert (
        again.headers["idempotent-replay"] == "true"
        and "idempotent-replay" not in first.headers
    )
    assert len(corrections(h)) == 1
    different = edit(h, headers, add=["Gamma"])  # same ID, other content
    assert (different.status_code, different.json()["code"]) == (
        409,
        "client_id_conflict",
    )
    assert len(corrections(h)) == 1


def test_a_crash_between_the_fabric_commit_and_the_journal_does_not_duplicate(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")
    first = edit(h, headers, add=["Beta"])
    operation_id = first.json()["operation_id"]
    # Roll the journal back as if the process died right after the Fabric append.
    h.store.update_operation(operation_id, "queued")
    with h.store._connect() as db:
        db.execute(
            "UPDATE operations SET payload=? WHERE operation_id=?",
            (
                '{"thread_ids":["record"],"add":["Beta"],"remove":[],"kinds":{}}',
                operation_id,
            ),
        )
    again = edit(h, headers, add=["Beta"])
    assert again.status_code == 200 and again.json()["state"] == "applied"
    assert len(corrections(h)) == 1  # the deterministic ID found its own row


def test_tag_edit_endpoint_validation(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")
    for body in (
        {"thread_ids": ["record"], "add": ["Beta"]},  # no client ID
        {"client_id": "short", "thread_ids": ["record"]},
        {"client_id": "client-valid-01", "thread_ids": []},
        {"client_id": "client-valid-01", "thread_ids": ["record"], "bogus": 1},
        {"client_id": "client-valid-01", "thread_ids": ["record"], "add": [" "]},
    ):
        assert h.client.post(TAGS, json=body, headers=headers).status_code == 422
    assert h.client.post(TAGS, content=b"x" * 70000, headers=headers).status_code == 413


def snapshot(h):
    return ContextDictionary.open_readonly(
        h.config.state_root / "context.sqlite3"
    ).snapshot()


def test_dictionary_rename_alias_kind_and_merge(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")

    def post(op, client_id, **body):
        return h.client.post(
            f"{DICT}/{op}", json={"client_id": client_id, **body}, headers=headers
        )

    renamed = post("rename", "client-dict-001", entry="Alpha", name="Alpha2")
    assert renamed.status_code == 200 and renamed.json()["result"] == {"name": "Alpha2"}
    assert (
        snapshot(h).resolve("A").name == "Alpha2"
    )  # aliases and old name still resolve
    assert (
        post("alias-add", "client-dict-002", entry="Alpha2", alias="AA").status_code
        == 200
    )
    assert snapshot(h).resolve("AA").name == "Alpha2"
    assert (
        post("alias-remove", "client-dict-003", entry="Alpha2", alias="AA").status_code
        == 200
    )
    assert snapshot(h).resolve("AA") is None
    assert (
        post("kind", "client-dict-004", entry="Gamma", kind="organization").status_code
        == 200
    )
    assert snapshot(h).resolve("Gamma").kind == "organization"
    both = post("rename", "client-dict-005", entry="Beta", name="B2", kind="topic")
    assert both.json()["code"] == "one_change_required"
    different_kind = post("merge", "client-dict-006", entry="Gamma", target="Beta")
    assert different_kind.json()["code"] == "kind_choice_required"
    merged = post(
        "merge", "client-dict-007", entry="Gamma", target="Beta", kind="person"
    )
    assert merged.status_code == 200 and snapshot(h).resolve("Gamma").name == "Beta"
    assert (
        post("rename", "client-dict-008", entry="Nobody", name="X").json()["code"]
        == "entry_not_found"
    )
    assert post("explode", "client-dict-009", entry="a").status_code == 404
    clash = post("rename", "client-dict-010", entry="Beta", name="Alpha2")
    assert (clash.status_code, clash.json()["code"]) == (422, "invalid_edit")


def test_dictionary_edits_are_idempotent_by_client_id(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("edit")
    body = {"client_id": "client-dict-100", "entry": "Alpha", "name": "Alpha9"}
    first = h.client.post(f"{DICT}/rename", json=body, headers=headers)
    again = h.client.post(f"{DICT}/rename", json=body, headers=headers)
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json() and again.headers["idempotent-replay"] == "true"
    other = h.client.post(
        f"{DICT}/rename", json={**body, "name": "Alpha10"}, headers=headers
    )
    assert other.json()["code"] == "client_id_conflict"


def test_edit_service_is_serialized_for_concurrent_duplicate_requests(tmp_path):
    import threading

    h = Harness(tmp_path)
    seed(h)
    service = EditService(h.config, h.store)
    results = []

    def run():
        edit_request = MobileTagEdit(
            client_id="client-race-0001", thread_ids=["record"], add=["Beta"]
        )
        results.append(service.tags(h.device.device_id, edit_request)[0].operation_id)

    h.pair("edit")
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1 and len(corrections(h)) == 1
