"""Feed kinds added for Alert, pipeline and Wiki: scope, negotiation, rollback."""

import json
from datetime import timedelta

from server.tests.alert.test_delivery import alert

from weaver.alert import AlertHistory
from weaver.mobile import api as mobile_api
from weaver.mobile.feed import FeedSources
from weaver.mobile.transport import reconcile_transports
from weaver.wiki.store import WikiStore

from .helpers import CAP, CAP2, NOW, SID, Harness, receive_pipeline

FEED = "/mobile/v1/changes"
ALL = "review,action,alert,pipeline,wiki,fabric"
BOTH = '[mobile.features]\nreview = "http"\nalert = "http"\npipeline = "http"\n'


def pull(h, headers, cursor=None, kinds=ALL, limit=100):
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if kinds is not None:
        params["kinds"] = kinds
    return h.client.get(FEED, params=params, headers=headers)


def kinds_of(response):
    return sorted((i["kind"], i["id"], i["op"]) for i in response.json()["items"])


def seed(h):
    """One alert, one pipeline observation and one Wiki page."""
    history = AlertHistory(h.config.state_root / "alerts.sqlite3")
    body = alert()
    history.stage(body, "thr_1")
    history.projected(body.alert_id)
    journal = receive_pipeline(h)
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    store.write("pages/네트워크.md", {"kind": "area"}, "사내망 영역", None)
    return history, body, journal, store


def test_new_kinds_carry_hints_only_with_scope_and_etag(tmp_path):
    h = Harness(tmp_path, BOTH)
    history, body, journal, _ = seed(h)
    everything = h.pair("read", "review", "action", label="all")
    page = pull(h, everything)
    items = {(i["kind"], i["id"]): i for i in page.json()["items"]}
    assert {k[0] for k in items} == {"alert", "pipeline", "wiki", "fabric"}
    assert ("alert", body.alert_id) in items and ("pipeline", SID) in items
    alert_item = items[("alert", body.alert_id)]
    # The body and the receipt stay on /alerts; the feed is only a hint.
    assert alert_item["body"] is None and alert_item["revision"] is None
    pipeline_item = items[("pipeline", SID)]
    detail = h.client.get(f"/mobile/v1/read/pipeline/{SID}", headers=everything)
    assert pipeline_item["revision"] == 1
    assert pipeline_item["body"] == {"etag": detail.headers["etag"]}
    (wiki,) = [i for i in items.values() if i["kind"] == "wiki"]
    page_etag = h.client.get(
        f"/mobile/v1/read/wiki/pages/{wiki['id']}", headers=everything
    ).headers["etag"]
    assert wiki["body"] == {"etag": page_etag}
    # Alert needs review (not the review transport); pipeline and wiki need read.
    review_only = pull(h, h.pair("review", label="r"))
    assert [k[0] for k in kinds_of(review_only)] == ["alert"]
    read_only = pull(h, h.pair("read", label="p"))
    assert [k[0] for k in kinds_of(read_only)] == ["fabric", "pipeline", "wiki"]


def test_older_phone_never_receives_kinds_added_after_it_shipped(tmp_path):
    h = Harness(tmp_path, BOTH)
    seed(h)
    headers = h.pair("read", "review", "action")
    legacy = pull(h, headers, kinds=None)
    assert kinds_of(legacy) == [("fabric", "threads", "upsert")]
    # Unknown names from a newer phone are ignored, not an error.
    odd = pull(h, headers, kinds="fabric,hologram")
    assert odd.status_code == 200 and kinds_of(odd) == [("fabric", "threads", "upsert")]
    assert kinds_of(pull(h, headers, kinds="")) == []
    caps = h.client.get("/mobile/v1/capabilities", headers=headers).json()
    assert caps["feed_kinds"] == ["alert", "fabric", "pipeline", "review", "wiki"]
    assert caps["features"]["pipeline"] is True
    assert caps["transports"]["pipeline"] == "http"
    assert caps["schema_versions"]["pipeline"] == 2


def test_revision_and_etag_follow_pipeline_and_wiki_changes(tmp_path):
    h = Harness(tmp_path, BOTH)
    _, _, journal, store = seed(h)
    headers = h.pair("read")
    first = pull(h, headers)
    cursor = first.json()["next_cursor"]
    journal.update(SID, location="loom", status="working")
    page = pull(h, headers, cursor)
    assert [(i["kind"], i["revision"]) for i in page.json()["items"]] == [
        ("pipeline", 2)
    ]
    assert kinds_of(pull(h, headers, page.json()["next_cursor"])) == []
    store.write("pages/보안.md", {"kind": "area"}, "보안 영역", None)
    wiki = pull(h, headers, page.json()["next_cursor"])
    assert [(i["kind"], i["op"]) for i in wiki.json()["items"]] == [("wiki", "upsert")]
    receive_pipeline(h, "src_01ARZ3NDEKTSV4RRFFQ69G5FAW", CAP2)
    added = pull(h, headers, wiki.json()["next_cursor"])
    assert kinds_of(added) == [("pipeline", "src_01ARZ3NDEKTSV4RRFFQ69G5FAW", "upsert")]


def test_expired_alert_body_is_a_tombstone_and_acks_stay_off_the_feed(tmp_path):
    h = Harness(tmp_path, BOTH)
    history, body, _, _ = seed(h)
    headers = h.pair("review")
    cursor = pull(h, headers).json()["next_cursor"]
    history.ack(h.device.device_id, body.alert_id, NOW)
    assert kinds_of(pull(h, headers, cursor)) == []  # a receipt is not a feed event
    history.collect(30, [h.device.device_id], NOW + timedelta(days=31))
    page = pull(h, headers, cursor)
    assert kinds_of(page) == [("alert", body.alert_id, "tombstone")]


def test_route_back_to_files_hides_kinds_without_tombstones_and_returns(tmp_path):
    on = Harness(tmp_path, BOTH)
    history, body, _, _ = seed(on)
    headers = on.pair("read", "review")
    cursor = pull(on, headers).json()["next_cursor"]
    recorded = on.store.feed_current()
    assert ("alert", body.alert_id) in recorded and ("pipeline", SID) in recorded
    off = Harness(tmp_path, "")  # both features back on files, same state
    files = off.pair("read", "review", label="again")
    page = pull(off, files)
    # Wiki has no files route, so it stays on the feed.
    assert [k[0] for k in kinds_of(page)] == ["fabric", "wiki"]
    assert pull(off, files, cursor).json()["items"] == []
    after = off.store.feed_current()
    assert after[("alert", body.alert_id)].op == "upsert"
    assert after[("pipeline", SID)].op == "upsert"
    assert not [e for e in after.values() if e.op == "tombstone"]
    # The HTTP bodies are closed too, so the phone cannot read both routes.
    assert off.client.get("/mobile/v1/read/pipeline", headers=files).status_code == 409
    # Back on http: a phone may have taken its snapshot while the kinds were
    # hidden, so every current entry of the returning kinds is announced anew.
    head = off.store.feed_head()
    back = Harness(tmp_path, BOTH)
    again = back.pair("read", "review", label="back")
    page = pull(back, again, cursor)
    assert kinds_of(page) == [
        ("alert", body.alert_id, "upsert"),
        ("pipeline", SID, "upsert"),
    ]
    assert all(i["seq"] > head for i in page.json()["items"])
    # Announced once: the next pass does not repeat them.
    assert kinds_of(pull(back, again, page.json()["next_cursor"])) == []


def test_actions_leaving_http_are_hidden_not_tombstoned(tmp_path):
    from weaver.action import ActionJob

    from .helpers import schedule

    h = Harness(tmp_path, 'transport = "http"\n')
    h.fabric.append([schedule("s1")])
    ActionJob(h.fabric, h.outbox, h.actions).run()
    headers = h.pair("read", "action", executor=True)
    assert any(i["kind"] == "action" for i in pull(h, headers).json()["items"])
    files = Harness(tmp_path, '[mobile.features]\naction = "files"\n')
    pull(files, headers)
    assert all(
        e.op == "upsert"
        for k, e in files.store.feed_current().items()
        if k[0] == "action"
    )
    assert all(i["kind"] != "action" for i in pull(files, headers).json()["items"])


def test_unreadable_sources_never_become_deletions(tmp_path):
    h = Harness(tmp_path, BOTH)
    _, body, _, store = seed(h)
    headers = h.pair("read", "review")
    pull(h, headers)
    producer = mobile_api.FeedProducer(
        h.store,
        h.reviews,
        h.actions,
        lambda: 0,
        sources=FeedSources(
            alerts=AlertHistory(h.config.state_root / "alerts.sqlite3"),
            alerts_http=lambda: True,
            pipeline=type("Gone", (), {"available": lambda self: False})(),
            pipeline_http=lambda: True,
            wiki_hints=lambda: None,
        ),
    )
    assert producer.reconcile() == 0
    assert not [e for e in h.store.feed_current().values() if e.op == "tombstone"]
    producer.sources = FeedSources(wiki_hints=lambda: {})
    producer.reconcile()
    # An empty but readable Wiki is a real removal.
    assert [k for k, e in h.store.feed_current().items() if e.op == "tombstone"] == [
        k for k in h.store.feed_current() if k[0] == "wiki"
    ]


def test_page_breaks_at_the_byte_budget_and_resumes(tmp_path, monkeypatch):
    h = Harness(tmp_path, BOTH)
    for index in range(6):
        h.add_package(f"pkg_{index}")
    headers = h.pair("read", "review")
    monkeypatch.setattr(mobile_api, "MAX_FEED_BYTES", 300)
    seen, cursor, pages = [], None, 0
    while True:
        page = pull(h, headers, cursor, limit=100).json()
        pages += 1
        assert page["items"] or not page["has_more"]
        seen += [(i["kind"], i["id"]) for i in page["items"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert pages > 1
    assert sorted(seen) == sorted(
        [("fabric", "threads"), *(("review", f"pkg_{i}") for i in range(6))]
    )


def test_stale_epoch_and_future_cursor_still_ask_for_a_new_snapshot(tmp_path):
    h = Harness(tmp_path, BOTH)
    seed(h)
    headers = h.pair("read", "review")
    epoch = pull(h, headers).json()["feed_epoch"]
    for cursor in (f"{epoch}.9999", "ep_" + "0" * 32 + ".1"):
        gone = pull(h, headers, cursor)
        assert (gone.status_code, gone.json()["code"]) == (410, "cursor_expired")


def test_read_pipeline_pages_etag_and_generation(tmp_path):
    h = Harness(tmp_path, BOTH)
    journal = receive_pipeline(h)
    receive_pipeline(h, "src_01ARZ3NDEKTSV4RRFFQ69G5FAW", CAP2)
    headers = h.pair("read")
    first = h.client.get(
        "/mobile/v1/read/pipeline", params={"limit": 1}, headers=headers
    )
    body = first.json()
    assert [i["source_id"] for i in body["items"]] == [SID]
    second = h.client.get(
        "/mobile/v1/read/pipeline",
        params={"limit": 1, "cursor": body["next_cursor"]},
        headers=headers,
    )
    assert second.json()["next_cursor"] is None
    assert "capture_hosts" not in second.json()["items"][0]  # default key omitted
    journal.update(SID, location="loom", status="working")
    stale = h.client.get(
        "/mobile/v1/read/pipeline",
        params={"limit": 1, "cursor": body["next_cursor"]},
        headers=headers,
    )
    assert (stale.status_code, stale.json()["code"]) == (410, "cursor_expired")
    bad = h.client.get(
        "/mobile/v1/read/pipeline", params={"cursor": "x"}, headers=headers
    )
    assert bad.status_code == 422
    item = h.client.get(f"/mobile/v1/read/pipeline/{SID}", headers=headers)
    assert item.json()["revision"] == 2
    cached = h.client.get(
        f"/mobile/v1/read/pipeline/{SID}",
        headers={**headers, "If-None-Match": item.headers["etag"]},
    )
    assert cached.status_code == 304
    assert (
        h.client.get("/mobile/v1/read/pipeline/nope", headers=headers).status_code
        == 404
    )
    assert (
        h.client.get(
            "/mobile/v1/read/pipeline", headers=h.pair("capture", label="c")
        ).status_code
        == 403
    )
    assert json.loads(item.text)["source_id"] == SID


def test_pipeline_rollback_republishes_the_file_projection(tmp_path):
    on = Harness(tmp_path, BOTH)
    receive_pipeline(on)
    assert (
        reconcile_transports(
            on.config.mobile, on.store, on.reviews, on.actions, on.config
        )
        == []
    )
    off = Harness(tmp_path, "")
    path = off.config.courier.proposals / "pipeline" / f"{SID}.json"
    assert not path.exists()
    assert "pipeline" in reconcile_transports(
        off.config.mobile, off.store, off.reviews, off.actions, off.config
    )
    assert json.loads(path.read_text())["source_id"] == SID


def test_pipeline_transport_requires_explicit_opt_in(tmp_path):
    h = Harness(tmp_path, 'transport = "http"\n')
    assert h.config.mobile.transport_for("pipeline") == "files"
    headers = h.pair("read")
    caps = h.client.get("/mobile/v1/capabilities", headers=headers).json()
    assert caps["features"]["pipeline"] is False
    assert "pipeline" not in caps["transports"]
    assert "pipeline" not in caps["schema_versions"]
    assert (
        h.client.get("/mobile/v1/read/pipeline", headers=headers).json()["code"]
        == "transport_files"
    )


# ---- review on files, cursor pin, bytes, broken rows, older APK ---------------


def test_review_on_files_leaves_the_feed_without_a_tombstone_and_returns(tmp_path):
    on = Harness(tmp_path, BOTH)
    on.add_package("pkg_a")
    headers = on.pair("read", "review")
    first = pull(on, headers)
    assert ("review", "pkg_a", "upsert") in kinds_of(first)
    cursor = first.json()["next_cursor"]
    off = Harness(tmp_path, '[mobile.features]\nalert = "http"\n')
    files = off.pair("read", "review", label="files")
    assert (
        "review"
        not in off.client.get("/mobile/v1/capabilities", headers=files).json()[
            "feed_kinds"
        ]
    )
    assert [k[0] for k in kinds_of(pull(off, files))] == ["fabric"]
    off.add_package("pkg_b")  # appears while review is on files: not announced
    assert kinds_of(pull(off, files, cursor)) == []
    assert not [e for e in off.store.feed_current().values() if e.op == "tombstone"]
    back = Harness(tmp_path, BOTH)
    again = back.pair("read", "review", label="again")
    page = pull(back, again, cursor)
    assert kinds_of(page) == [
        ("review", "pkg_a", "upsert"),
        ("review", "pkg_b", "upsert"),
    ]


def test_cursor_is_pinned_to_the_requested_kinds(tmp_path):
    h = Harness(tmp_path, BOTH)
    seed(h)
    headers = h.pair("read", "review")
    cursor = pull(h, headers).json()["next_cursor"]
    assert pull(h, headers, cursor).status_code == 200
    for other in ("fabric", "pipeline,fabric", "", None):
        gone = pull(h, headers, cursor, kinds=other)
        assert (gone.status_code, gone.json()["code"]) == (410, "cursor_expired"), other
    # Order and unknown names do not change the set.
    same = "fabric,wiki,pipeline,alert,review,action,hologram"
    assert pull(h, headers, cursor, kinds=same).status_code == 200
    # A cursor of the old format (no pin) is valid for the legacy set only.
    epoch, seq, _ = cursor.split(".")
    assert pull(h, headers, f"{epoch}.{seq}", kinds=None).status_code == 200
    assert pull(h, headers, f"{epoch}.{seq}").status_code == 410


def _bytes(entry):
    return len(mobile_api.canonical(entry).encode())


def test_page_size_counts_utf8_bytes_not_characters(tmp_path, monkeypatch):
    h = Harness(tmp_path, BOTH)
    for index in range(3):
        h.add_package(f"pkg_{index}")
    headers = h.pair("read", "review")
    entries = pull(h, headers, kinds="review").json()["items"]
    chars = len(mobile_api.canonical(entries[0]))
    assert _bytes(entries[0]) == chars + 4  # stage "검토": 3 bytes per character
    # Two entries fit by character count, but not by bytes: one item per page.
    monkeypatch.setattr(mobile_api, "MAX_FEED_BYTES", 2 * chars + 4)
    page = pull(h, headers, kinds="review").json()
    assert len(page["items"]) == 1 and page["has_more"] is True
    monkeypatch.setattr(mobile_api, "MAX_FEED_BYTES", 2 * _bytes(entries[0]))
    assert len(pull(h, headers, kinds="review").json()["items"]) == 2


def test_first_item_over_the_budget_is_still_sent_and_progress_is_made(
    tmp_path, monkeypatch
):
    h = Harness(tmp_path, BOTH)
    seed(h)
    headers = h.pair("read", "review")
    monkeypatch.setattr(mobile_api, "MAX_FEED_BYTES", 1)
    seen, cursor = [], None
    for _ in range(20):
        page = pull(h, headers, cursor).json()
        assert len(page["items"]) <= 1
        seen += [(i["kind"], i["id"]) for i in page["items"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    else:
        raise AssertionError("never finished")
    assert len(seen) == 4 and len(set(seen)) == 4


def test_filtered_items_at_a_page_boundary_neither_stall_nor_get_skipped(
    tmp_path, monkeypatch
):
    h = Harness(tmp_path, BOTH)
    for index in range(3):
        h.add_package(f"pkg_{index}")
    seed(h)
    headers = h.pair("read")  # reviews and alerts are filtered out by scope
    full = pull(h, headers).json()
    wanted = [(i["kind"], i["id"]) for i in full["items"]]
    assert {k for k, _ in wanted} == {"fabric", "pipeline", "wiki"}
    one = _bytes(full["items"][0])
    monkeypatch.setattr(mobile_api, "MAX_FEED_BYTES", one + 1)
    seen, cursor = [], None
    for _ in range(20):
        page = pull(h, headers, cursor, limit=3).json()
        seen += [(i["kind"], i["id"]) for i in page["items"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    else:
        raise AssertionError("never finished")
    assert sorted(seen) == sorted(wanted)


def test_not_modified_replies_are_not_cached(tmp_path):
    h = Harness(tmp_path, BOTH)
    receive_pipeline(h)
    headers = h.pair("read")
    etag = h.client.get(f"/mobile/v1/read/pipeline/{SID}", headers=headers).headers[
        "etag"
    ]
    cached = h.client.get(
        f"/mobile/v1/read/pipeline/{SID}", headers={**headers, "If-None-Match": etag}
    )
    assert cached.status_code == 304
    assert cached.headers["cache-control"] == "no-store"
    assert cached.headers["etag"] == etag


def test_one_broken_pipeline_row_is_skipped_and_never_fails_the_feed(tmp_path, caplog):
    import sqlite3

    h = Harness(tmp_path, BOTH)
    receive_pipeline(h)
    receive_pipeline(h, "src_01ARZ3NDEKTSV4RRFFQ69G5FAW", CAP2)
    headers = h.pair("read")
    cursor = pull(h, headers).json()["next_cursor"]
    db = sqlite3.connect(h.config.state_root / "pipeline.sqlite3")
    with db:
        db.execute(
            "UPDATE items SET payload='{\"nonsense\": 1}' WHERE source_id=?", (SID,)
        )
    db.close()
    with caplog.at_level("WARNING", logger="weaver.mobile"):
        page = pull(h, headers, cursor)
    assert page.status_code == 200
    assert f"pipeline row skipped: {SID} ValidationError" in caplog.text
    # The broken row still exists, so it is not tombstoned; the healthy one stays.
    current = h.store.feed_current()
    assert current[("pipeline", SID)].op == "upsert"
    assert current[("pipeline", "src_01ARZ3NDEKTSV4RRFFQ69G5FAW")].op == "upsert"
    listing = h.client.get("/mobile/v1/read/pipeline", headers=headers).json()
    assert [i["source_id"] for i in listing["items"]] == [
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"
    ]
    assert (
        h.client.get(f"/mobile/v1/read/pipeline/{SID}", headers=headers).status_code
        == 404
    )


def test_unreadable_pipeline_database_keeps_entries_and_answers_503_on_read(
    tmp_path,
):
    h = Harness(tmp_path, BOTH)
    receive_pipeline(h)
    headers = h.pair("read")
    pull(h, headers)
    (h.config.state_root / "pipeline.sqlite3").write_bytes(b"this is not sqlite" * 64)
    page = pull(h, headers)
    assert page.status_code == 200
    assert h.store.feed_current()[("pipeline", SID)].op == "upsert"
    bad = h.client.get("/mobile/v1/read/pipeline", headers=headers)
    assert (bad.status_code, bad.json()["retryable"]) == (503, True)


def test_pipeline_connection_is_closed(tmp_path, monkeypatch):
    import sqlite3

    from weaver.mobile import pipeline as module

    h = Harness(tmp_path, BOTH)
    receive_pipeline(h)
    opened = []
    real = sqlite3.connect

    class Spy:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.closed = True
            self.connection.close()

    def connect(*args, **kwargs):
        spy = Spy(real(*args, **kwargs))
        opened.append(spy)
        return spy

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    module.PipelineReadModel(h.config.state_root).snapshot()
    assert opened and all(spy.closed for spy in opened)


def test_older_apk_pulls_the_pipeline_list_from_the_compat_path(tmp_path):
    """An APK before the feed client reads features.pipeline and GETs /pipeline."""
    h = Harness(tmp_path, BOTH)
    for sid, cap in ((SID, CAP), ("src_01ARZ3NDEKTSV4RRFFQ69G5FAW", CAP2)):
        receive_pipeline(h, sid, cap)
    apk = h.pair("read", "review", label="older")
    caps = h.client.get("/mobile/v1/capabilities", headers=apk).json()
    assert caps["features"]["pipeline"] is True  # the key it already understands
    names, cursor, limit = [], None, 1
    while True:
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        page = h.client.get("/mobile/v1/pipeline", params=params, headers=apk)
        assert page.status_code == 200
        names += [i["source_id"] for i in page.json()["items"]]
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break
    modern = h.client.get("/mobile/v1/read/pipeline", headers=apk).json()
    assert names == [i["source_id"] for i in modern["items"]] and len(names) == 2
    # Same source as /read/pipeline, same gate: files route closes it.
    off = Harness(tmp_path / "off")
    assert (
        off.client.get("/mobile/v1/pipeline", headers=off.pair("review")).status_code
        == 409
    )
    assert (
        h.client.get("/mobile/v1/pipeline", headers=h.pair("read", label="r")).json()[
            "code"
        ]
        == "scope_denied"
    )


def test_alert_hint_ids_are_the_ids_alerts_serves(tmp_path):
    """Hint basis: delivery rows whose body is held, as AlertHistory.bodies/.body."""
    h = Harness(tmp_path, BOTH)
    history, body, _, _ = seed(h)
    pending = alert(1)
    history.stage(pending, "thr_1")  # staged, never projected: /alerts serves it
    gone = alert(2)
    history.stage(gone, "thr_1")
    history.projected(gone.alert_id)
    headers = h.pair("review")
    # Another device acked it and the retention window passed: the body expired.
    history.ack("dev_other", gone.alert_id, NOW)
    history.collect(30, ["dev_other"], NOW + timedelta(days=31))
    served = {
        a["alert_id"]
        for a in h.client.get("/mobile/v1/alerts", headers=headers).json()["items"]
    }
    hinted = {
        i["id"]
        for i in pull(h, headers, kinds="alert").json()["items"]
        if i["op"] == "upsert"
    }
    assert hinted == served == {body.alert_id, pending.alert_id}
