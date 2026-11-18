"""Change feed: persistent seq/epoch, compaction, cursors and checkpoints."""

from weaver.action import ActionJob

from .helpers import Harness, result, schedule, verdict

FEED = "/mobile/v1/changes"


def pull(h, headers, cursor=None, limit=100):
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return h.client.get(FEED, params=params, headers=headers)


def kinds(response):
    return [(i["kind"], i["id"], i["op"]) for i in response.json()["items"]]


def test_feed_reports_reviews_and_tombstones_them_when_applied(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read", "review")
    first = pull(h, headers)
    assert kinds(first) == [("fabric", "threads", "upsert")]
    cursor = first.json()["next_cursor"]
    assert cursor.startswith(first.json()["feed_epoch"] + ".")
    h.add_package("pkg_a")
    page = pull(h, headers, cursor)
    assert kinds(page) == [("review", "pkg_a", "upsert")]
    item = page.json()["items"][0]
    assert item["revision"] == 1 and item["body"]["stage"] == "검토"
    cursor = page.json()["next_cursor"]
    assert kinds(pull(h, headers, cursor)) == []  # nothing new, same cursor
    h.add_package("pkg_a", 2)
    page = pull(h, headers, cursor)
    assert [(i["id"], i["revision"]) for i in page.json()["items"]] == [("pkg_a", 2)]
    # An applied review leaves the feed as an explicit tombstone.
    etag = h.client.get(
        "/mobile/v1/reviews/pkg_a/revisions/2", headers=headers
    ).headers["etag"]
    h.client.put(
        "/mobile/v1/reviews/pkg_a/revisions/2/verdict",
        json=verdict(revision=2),
        headers={**headers, "If-Match": etag},
    )
    h.applier.run()
    page = pull(h, headers, page.json()["next_cursor"])
    assert kinds(page) == [("review", "pkg_a", "tombstone")]


def test_feed_is_compacted_and_from_cursor_zero_is_a_snapshot(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read", "review")
    for revision in (1, 2, 3):
        h.add_package("pkg_a", revision)
        pull(h, headers)
    h.add_package("pkg_b")
    snapshot = pull(h, headers)
    assert sorted(kinds(snapshot)) == [
        ("fabric", "threads", "upsert"),
        ("review", "pkg_a", "upsert"),
        ("review", "pkg_b", "upsert"),
    ]
    revisions = {i["id"]: i["revision"] for i in snapshot.json()["items"]}
    assert revisions["pkg_a"] == 3
    seqs = [i["seq"] for i in snapshot.json()["items"]]
    assert seqs == sorted(seqs)


def test_pagination_and_scope_filtering_do_not_stall_the_cursor(tmp_path):
    h = Harness(tmp_path)
    for index in range(5):
        h.add_package(f"pkg_{index}")
    reader = h.pair("read")  # no review scope: sees only the fabric hint
    page = pull(h, reader, limit=2)
    assert page.json()["has_more"] is True
    assert page.json()["items"] == [] or all(
        i["kind"] == "fabric" for i in page.json()["items"]
    )
    seen, cursor = [], None
    for _ in range(10):
        page = pull(h, reader, cursor, limit=2)
        seen += kinds(page)
        cursor = page.json()["next_cursor"]
        if not page.json()["has_more"]:
            break
    assert seen == [("fabric", "threads", "upsert")]
    reviewer = h.pair("read", "review", label="tab")
    total, cursor = [], None
    for _ in range(10):
        page = pull(h, reviewer, cursor, limit=2)
        total += [i["id"] for i in page.json()["items"] if i["kind"] == "review"]
        cursor = page.json()["next_cursor"]
        if not page.json()["has_more"]:
            break
    assert sorted(total) == [f"pkg_{i}" for i in range(5)]


def test_unknown_epoch_future_cursor_and_garbage_cursor(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    good = pull(h, headers).json()["next_cursor"]
    epoch = good.split(".")[0]
    for cursor in ("ep_" + "0" * 32 + ".1", f"{epoch}.9999"):
        gone = pull(h, headers, cursor)
        assert (gone.status_code, gone.json()["code"]) == (410, "cursor_expired")
    for cursor in ("garbage", "1.2", f"{epoch}.-1"):
        assert pull(h, headers, cursor).status_code == 422
    assert h.client.get(FEED, params={"limit": 101}, headers=headers).status_code == 422


def test_server_restore_changes_the_epoch_so_phones_resync(tmp_path):
    first = Harness(tmp_path / "a")
    headers = first.pair("read")
    cursor = pull(first, headers).json()["next_cursor"]
    second = Harness(tmp_path / "b")  # a fresh state directory: a new feed epoch
    headers = second.pair("read")
    assert pull(second, headers, cursor).status_code == 410
    assert first.store.feed_epoch != second.store.feed_epoch


def test_checkpoint_is_monotonic_and_validated_and_never_trusts_a_body_device(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    h.add_package("pkg_a")
    cursor = pull(h, headers).json()["next_cursor"]
    ok = h.client.put(
        "/mobile/v1/devices/self/checkpoint", json={"cursor": cursor}, headers=headers
    )
    assert ok.status_code == 200
    epoch, seq = cursor.split(".")
    h.client.put(
        "/mobile/v1/devices/self/checkpoint",
        json={"cursor": f"{epoch}.0"},
        headers=headers,
    )
    assert h.store.checkpoint(h.device.device_id) == (
        epoch,
        int(seq),
    )  # never goes back
    ahead = h.client.put(
        "/mobile/v1/devices/self/checkpoint",
        json={"cursor": f"{epoch}.999"},
        headers=headers,
    )
    assert ahead.status_code == 410
    forged = h.client.put(
        "/mobile/v1/devices/self/checkpoint",
        json={"cursor": cursor, "device_id": "dev_other"},
        headers=headers,
    )
    assert forged.status_code == 200 and h.store.checkpoint("dev_other") is None
    assert (
        h.client.put(
            "/mobile/v1/devices/self/checkpoint", json={}, headers=headers
        ).status_code
        == 422
    )


def test_actions_enter_the_feed_without_leaking_content(tmp_path):
    h = Harness(tmp_path, 'transport = "http"\n')
    job = ActionJob(h.fabric, h.outbox, h.actions)
    h.fabric.append([schedule("s1")])
    job.run()
    fold = h.pair("read", "action", executor=True)
    page = pull(h, fold)
    (action,) = [i for i in page.json()["items"] if i["kind"] == "action"]
    assert action["body"] is None and action["op"] == "upsert"
    h.client.put(
        f"/mobile/v1/actions/{action['id']}/result",
        json=result(action["id"]),
        headers=fold,
    )
    h.applier.run()
    later = pull(h, fold, page.json()["next_cursor"])
    assert ("action", action["id"], "tombstone") in kinds(later)
    # A device without the action scope never sees action entries.
    reader = h.pair("read", label="reader")
    assert all(i["kind"] != "action" for i in pull(h, reader).json()["items"])


def test_feed_survives_a_restart_without_losing_or_repeating_changes(tmp_path):
    from weaver.mobile.store import MobileStore

    h = Harness(tmp_path)
    headers = h.pair("read", "review")
    h.add_package("pkg_a")
    page = pull(h, headers)
    cursor, head = page.json()["next_cursor"], h.store.feed_head()
    h.add_package("pkg_b")  # changed while the producer was not running
    reopened = MobileStore(h.config.state_root)  # the same file after a restart
    assert (reopened.feed_epoch, reopened.feed_head()) == (h.store.feed_epoch, head)
    assert kinds(pull(h, headers, cursor)) == [("review", "pkg_b", "upsert")]
    assert kinds(pull(h, headers, pull(h, headers, cursor).json()["next_cursor"])) == []
