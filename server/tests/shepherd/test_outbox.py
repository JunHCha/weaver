"""Private synthetic Courier recovery; no user data or external requests."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from weaver.schema import Manifest
from weaver.shepherd.outbox import BundleKey, CapacityError, Outbox, OutboxLimits


def manifest(key: BundleKey, body: bytes) -> Manifest:
    return Manifest.model_validate(
        {
            "schema_version": 1,
            "capture_id": key.capture_id,
            "capture_host": "mac",
            "kind": "text",
            "channel": "share_text",
            "payload": key.capture_id + ".txt",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "captured_at": "2026-11-20T00:00:00Z",
            "occurred_at": None,
            "hints": {"counterpart": None},
        }
    )


def test_buffer_cursor_capacity_and_restart(tmp_path: Path) -> None:
    notices = []
    key = BundleKey("one", "space", "2026-11-20", 0, 0)
    box = Outbox(
        tmp_path / "outbox.db", "one", OutboxLimits(1, 10000, 1), notify=notices.append
    )
    box.buffer(key, b"first", cursor=("fetch", "page1"))
    with pytest.raises(CapacityError):
        box.buffer(
            BundleKey("one", "other", "2026-11-20", 0, 0),
            b"second",
            cursor=("fetch", "page2"),
        )
    assert box.cursor("fetch") == "page1"
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(1, 10000, 1))
    box.close(key, manifest(key, b"first"))
    assert box.get(key.capture_id).payload == b"first"
    with pytest.raises(ValueError):
        box.buffer(key, b"changed")
    assert notices == ["outbox_capacity"]


def test_ids_are_unambiguous_and_instance_scoped() -> None:
    a = BundleKey("a", "bc", "2026-11-20", 0, 0)
    assert a.capture_id == BundleKey("a", "bc", "2026-11-20", 0, 0).capture_id
    assert a.capture_id != BundleKey("ab", "c", "2026-11-20", 0, 0).capture_id
    assert a.capture_id != BundleKey("b", "bc", "2026-11-20", 0, 0).capture_id
    manifest(a, b"test")


def test_lost_response_recovers_receipt_before_gc(tmp_path: Path) -> None:
    key = BundleKey("one", "space", "2026-11-20", 0, 0)
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    box.buffer(key, b"body")
    box.close(key, manifest(key, b"body"))
    calls = []
    stored = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal stored
        calls.append(request.method + " " + request.url.path)
        if request.method == "GET":
            return (
                httpx.Response(
                    200,
                    json={
                        "schema_version": 1,
                        "capture_id": key.capture_id,
                        "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                        "sha256": hashlib.sha256(b"body").hexdigest(),
                        "received_at": "2026-11-20T00:00:00Z",
                    },
                )
                if stored
                else httpx.Response(404)
            )
        if request.url.path.endswith("manifest"):
            stored = True
            raise httpx.ReadTimeout("lost", request=request)
        assert request.content == b"body"
        return httpx.Response(201)

    client = httpx.Client(
        base_url="http://127.0.0.1:8788/mobile/v1/",
        transport=httpx.MockTransport(respond),
    )
    box.deliver(client)
    assert box.get(key.capture_id).payload == b"body"
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    box.deliver(client)
    assert box.get(key.capture_id).receipt is not None
    assert box.get(key.capture_id).payload is None
    assert len(calls) == 4


def test_quarantine_blocks_correction_but_not_other_spaces(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    original = BundleKey("one", "a", "2026-11-20", 0, 0)
    correction = BundleKey("one", "a", "2026-11-20", 0, 1)
    other = BundleKey("one", "b", "2026-11-20", 0, 0)
    for key in (original, correction, other):
        box.buffer(key, b"body")
        box.close(
            key,
            manifest(key, b"body"),
            depends_on=original.capture_id if key == correction else None,
        )
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return (
            httpx.Response(404)
            if request.method == "GET"
            else httpx.Response(
                409, json={"code": "capture_conflict", "retryable": False}
            )
        )

    box.deliver(
        httpx.Client(
            base_url="http://test/mobile/v1/", transport=httpx.MockTransport(respond)
        )
    )
    assert box.get(original.capture_id).state == "quarantine"
    assert box.get(original.capture_id).payload == b"body"
    assert not any(correction.capture_id in path for path in sent)
    assert any(other.capture_id in path for path in sent)
    box.retry(original.capture_id)
    assert box.get(original.capture_id).state == "pending"
    box.discard(original.capture_id)
    assert box.get(original.capture_id).state == "discarded"
    assert box.get(correction.capture_id).state == "pending"


def test_disk_floor_and_auth_pause_survive_restart(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", free_bytes=lambda: 0)
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    with pytest.raises(CapacityError):
        box.buffer(key, b"body", cursor=("fetch", "page"))
    assert box.cursor("fetch") is None
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    box.buffer(key, b"body")
    box.close(key, manifest(key, b"body"))
    client = httpx.Client(
        base_url="http://test/",
        transport=httpx.MockTransport(lambda _: httpx.Response(401)),
    )
    box.deliver(client)
    assert Outbox(tmp_path / "outbox.db", "one").paused
    box.resume()
    assert not box.paused


@pytest.mark.parametrize("status", [409, 422])
def test_permanent_errors_retain_request_and_count_toward_capacity(
    tmp_path: Path, status: int
) -> None:
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(1, 10000, 1))
    box.buffer(key, b"body")
    frozen = manifest(key, b"body")
    box.close(key, frozen)
    box.close(key, frozen)

    def respond(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(404)
            if request.method == "GET"
            else httpx.Response(
                status, json={"code": "invalid_manifest", "retryable": False}
            )
        )

    box.deliver(
        httpx.Client(base_url="http://test/", transport=httpx.MockTransport(respond))
    )
    entry = box.get(key.capture_id)
    assert (
        entry.payload == b"body" and entry.manifest == frozen.model_dump_json().encode()
    )
    assert entry.error == f"http_{status}:invalid_manifest"
    with pytest.raises(CapacityError):
        box.buffer(BundleKey("one", "b", "2026-11-20", 0, 0), b"new")
    changed = frozen.model_copy(
        update={"captured_at": datetime(2026, 11, 21, tzinfo=UTC)}
    )
    with pytest.raises(ValueError, match="immutable_capture"):
        box.close(key, changed)


def test_receipt_validation_and_crash_between_receipt_and_gc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    box.buffer(key, b"body")
    box.close(key, manifest(key, b"body"))
    good = {
        "schema_version": 1,
        "capture_id": key.capture_id,
        "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "sha256": hashlib.sha256(b"body").hexdigest(),
        "received_at": "2026-11-20T00:00:00Z",
    }
    client = httpx.Client(
        base_url="http://test/",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={**good, "sha256": "0" * 64})
        ),
    )
    box.deliver(client)
    assert box.get(key.capture_id).state == "quarantine"
    assert box.get(key.capture_id).payload == b"body"
    box.retry(key.capture_id)
    original_gc = box.collect_garbage
    calls = 0

    def crash() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("power_cut_after_receipt")
        original_gc()

    monkeypatch.setattr(box, "collect_garbage", crash)
    client = httpx.Client(
        base_url="http://test/",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=good)),
    )
    with pytest.raises(RuntimeError, match="power_cut"):
        box.deliver(client)
    assert box.get(key.capture_id).receipt is not None
    assert box.get(key.capture_id).payload == b"body"
    restarted = Outbox(tmp_path / "outbox.db", "one")
    restarted.collect_garbage()
    assert restarted.get(key.capture_id).payload is None


def test_temporary_payload_missing_and_dependency_order(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    original = BundleKey("one", "a", "2026-11-20", 0, 0)
    correction = BundleKey("one", "a", "2026-11-20", 0, 1)
    for key in (original, correction):
        box.buffer(key, b"body")
    with pytest.raises(ValueError, match="correction_requires_dependency"):
        box.close(correction, manifest(correction, b"body"))
    box.close(original, manifest(original, b"body"))
    box.close(correction, manifest(correction, b"body"), depends_on=original.capture_id)
    first = True
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal first
        if request.method == "GET":
            return httpx.Response(404)
        if first and request.url.path.endswith("manifest"):
            first = False
            return httpx.Response(
                409, json={"code": "payload_missing", "retryable": True}
            )
        if request.url.path.endswith("manifest"):
            identifier = request.url.path.split("/")[-2]
            if identifier == correction.capture_id:
                assert box.get(original.capture_id).receipt is not None
            sent.append(identifier)
            return httpx.Response(
                201,
                json={
                    "schema_version": 1,
                    "capture_id": identifier,
                    "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "sha256": hashlib.sha256(b"body").hexdigest(),
                    "received_at": "2026-11-20T00:00:00Z",
                },
            )
        return httpx.Response(201)

    client = httpx.Client(
        base_url="http://test/", transport=httpx.MockTransport(respond)
    )
    box.deliver(client)
    assert box.get(original.capture_id).state == "pending"
    assert not sent
    box.deliver(client)
    assert sent == [original.capture_id, correction.capture_id]


def test_bytes_limit_close_transaction_and_collision_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(10, 5, 1))
    box.buffer(key, b"body", cursor=("fetch", "one"))
    with pytest.raises(CapacityError):
        box.close(key, manifest(key, b"body"))
    assert box.get(key.capture_id).state == "buffer"
    assert box.get(key.capture_id).payload == b"body"
    assert box.get(key.capture_id).manifest is None
    with pytest.raises(CapacityError):
        box.buffer(key, b"longer", cursor=("fetch", "two"))
    assert box.cursor("fetch") == "one"
    monkeypatch.setattr(BundleKey, "capture_id", property(lambda _: key_id))
    key_id = box.entries().__next__().capture_id
    with pytest.raises(ValueError, match="immutable_capture"):
        box.buffer(BundleKey("one", "collision", "2026-11-20", 0, 0), b"x")


def test_reached_count_limit_stops_existing_buffer_cursor(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(1, 10000, 1))
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box.buffer(key, b"one", cursor=("fetch", "one"))
    with pytest.raises(CapacityError):
        box.buffer(key, b"two", cursor=("fetch", "two"))
    assert box.cursor("fetch") == "one"
    assert box.get(key.capture_id).payload == b"one"
    box.close(key, manifest(key, b"one"))  # Existing work can still drain.


def test_correction_cannot_depend_on_unrelated_bundle(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    original = BundleKey("one", "a", "2026-11-20", 0, 0)
    correction = BundleKey("one", "b", "2026-11-20", 0, 1)
    box.buffer(original, b"body")
    box.close(original, manifest(original, b"body"))
    box.buffer(correction, b"body")
    with pytest.raises(ValueError, match="invalid_correction_dependency"):
        box.close(
            correction, manifest(correction, b"body"), depends_on=original.capture_id
        )


def test_revision_two_cannot_skip_pending_revision_one(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    keys = [BundleKey("one", "a", "2026-11-20", 0, revision) for revision in range(3)]
    for key in keys:
        box.buffer(key, b"body")
    box.close(keys[0], manifest(keys[0], b"body"))
    box.close(keys[1], manifest(keys[1], b"body"), depends_on=keys[0].capture_id)
    with pytest.raises(ValueError, match="invalid_correction_dependency"):
        box.close(keys[2], manifest(keys[2], b"body"), depends_on=keys[0].capture_id)
    box.close(keys[2], manifest(keys[2], b"body"), depends_on=keys[1].capture_id)
    visited = []
    allow_one = False

    def respond(request: httpx.Request) -> httpx.Response:
        identifier = request.url.path.split("/")[-2]
        visited.append(identifier)
        if request.method == "GET":
            if identifier == keys[1].capture_id and not allow_one:
                return httpx.Response(503)
            if identifier == keys[2].capture_id:
                assert (
                    box.get(keys[1].capture_id, include_payload=False).receipt
                    is not None
                )
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "capture_id": identifier,
                    "sha256": hashlib.sha256(b"body").hexdigest(),
                    "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "received_at": "2026-11-20T00:00:00Z",
                },
            )
        raise AssertionError("Receipt recovery must avoid uploading")

    client = httpx.Client(
        base_url="http://test/", transport=httpx.MockTransport(respond)
    )
    box.deliver(client)
    assert visited == [keys[0].capture_id, keys[1].capture_id]
    allow_one = True
    box.deliver(client)
    assert box.get(keys[2].capture_id).state == "received"


def test_discarded_revision_is_excluded_from_predecessor(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    keys = [BundleKey("one", "a", "2026-11-20", 0, revision) for revision in range(3)]
    for key in keys:
        box.buffer(key, b"body")
    box.close(keys[0], manifest(keys[0], b"body"))
    box.close(keys[1], manifest(keys[1], b"body"), depends_on=keys[0].capture_id)
    box.discard(keys[1].capture_id)
    with pytest.raises(ValueError, match="invalid_correction_dependency"):
        box.close(keys[2], manifest(keys[2], b"body"), depends_on=keys[1].capture_id)
    box.close(keys[2], manifest(keys[2], b"body"), depends_on=keys[0].capture_id)


@pytest.mark.parametrize("phase", ["receipt", "payload", "manifest"])
@pytest.mark.parametrize(
    "status,code",
    [
        (202, "unexpected_response"),
        (302, "unexpected_redirect"),
        (400, "invalid_host"),
        (400, "invalid_headers"),
        (402, "unknown_error"),
        (404, "unknown_error"),
        (405, "unknown_error"),
        (410, "unknown_error"),
        (418, "unknown_error"),
    ],
)
def test_unexpected_client_errors_pause_and_notify(
    tmp_path: Path, phase: str, status: int, code: str
) -> None:
    if phase == "receipt" and status == 404:
        return  # Receipt-not-found is the documented upload/recovery path.
    notices = []
    box = Outbox(
        tmp_path / "outbox.db",
        "one",
        OutboxLimits(min_free_bytes=1),
        notify=notices.append,
    )
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box.buffer(key, b"body")
    box.close(key, manifest(key, b"body"))
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith(phase):
            return httpx.Response(
                status,
                json={"code": code},
            )
        return httpx.Response(404 if request.method == "GET" else 201)

    client = httpx.Client(
        base_url="http://test/", transport=httpx.MockTransport(respond)
    )
    box.deliver(client)
    assert box.paused and notices == ["courier_operator_required"]
    entry = box.get(key.capture_id)
    assert (
        entry.state == "pending"
        and entry.payload == b"body"
        and f"http_{status}" in (entry.error or "")
    )
    before = len(calls)
    Outbox(box.path, "one").deliver(client)
    assert len(calls) == before


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_errors_remain_retryable(tmp_path: Path, status: int) -> None:
    notices = []
    box = Outbox(
        tmp_path / "outbox.db",
        "one",
        OutboxLimits(min_free_bytes=1),
        notify=notices.append,
    )
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box.buffer(key, b"body")
    box.close(key, manifest(key, b"body"))
    box.deliver(
        httpx.Client(
            base_url="http://test/",
            transport=httpx.MockTransport(lambda _: httpx.Response(status)),
        )
    )
    assert not box.paused and not notices and box.get(key.capture_id).payload == b"body"


@pytest.mark.parametrize("retryable", [True, False])
def test_receipt_get_409_recovery_or_quarantine(
    tmp_path: Path, retryable: bool
) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box.buffer(key, b"body")
    box.close(key, manifest(key, b"body"))
    uploaded = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                409,
                json={
                    "code": "payload_missing" if retryable else "capture_conflict",
                    "retryable": retryable,
                },
            )
        uploaded.append(request.url.path)
        return httpx.Response(503)  # Leave bytes available for the next replay.

    box.deliver(
        httpx.Client(base_url="http://test/", transport=httpx.MockTransport(respond))
    )
    assert bool(uploaded) == retryable
    assert box.get(key.capture_id).state == ("pending" if retryable else "quarantine")
    assert box.get(key.capture_id).payload == b"body"


def test_metadata_reads_do_not_load_payload_or_take_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    box.buffer(key, b"body", cursor=("fetch", "page"))
    box.close(key, manifest(key, b"body"))
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    # A RESERVED writer lock must not prevent a metadata reader.
    with box._write():
        statements.clear()
        assert box.get(key.capture_id, include_payload=False).payload is None
        assert list(box.entries())[0].payload is None
        assert box.cursor("fetch") == "page"
        assert not box.paused
        assert box.counts() == {"pending": 1}
        assert not any(
            "BEGIN" in query or "payload" in query.lower() for query in statements
        )


def test_incremental_vacuum_releases_gc_pages(tmp_path: Path) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    key = BundleKey("one", "a", "2026-11-20", 0, 0)
    body = b"x" * 2**20
    box.buffer(key, body)
    box.close(key, manifest(key, body))
    before = box.path.stat().st_size
    with box._write() as db:
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        db.execute(
            "UPDATE entries SET state='received', receipt=?",
            (b"durably verified receipt fixture",),
        )
    box.collect_garbage()
    assert box.path.stat().st_size < before // 2
    assert box.get(key.capture_id).payload is None


def _chain(box: Outbox, shape: list[int]) -> list[list[BundleKey]]:
    """Close revisions r0..rn with shape[r] parts each; every part follows its parent."""
    revisions = []
    for revision, parts in enumerate(shape):
        keys = [BundleKey("one", "a", "2026-11-20", n, revision) for n in range(parts)]
        for key in keys:
            box.buffer(key, b"body")
        for key in keys:
            box.close(
                key, manifest(key, b"body"), depends_on=box.correction_parent(key)
            )
        revisions.append(keys)
    return revisions


def _courier(blocked: set[str], quarantined: set[str] | None = None):
    """Fake Courier: a Receipt GET is a 503 for blocked parts; PUT may 409."""
    asked: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        identifier = request.url.path.split("/")[-2]
        asked.append(identifier)
        if request.method == "GET":
            if identifier in blocked:
                return httpx.Response(503)
            if identifier in (quarantined or set()):
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "capture_id": identifier,
                    "sha256": hashlib.sha256(b"body").hexdigest(),
                    "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "received_at": "2026-11-20T00:00:00Z",
                },
            )
        return httpx.Response(409, json={"code": "capture_conflict"})

    client = httpx.Client(
        base_url="http://test/", transport=httpx.MockTransport(respond)
    )
    return client, asked


@pytest.mark.parametrize("shape", [[2, 1], [1, 2], [1, 2, 1], [2, 2, 1], [1, 3, 2]])
def test_correction_waits_for_every_part_of_the_previous_revision(
    tmp_path: Path, shape: list[int]
) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    revisions = _chain(box, shape)
    # Every part of revision n depends on the anchor (first part) of revision n-1.
    for earlier, later in zip(revisions, revisions[1:], strict=False):
        assert {box.get(k.capture_id).depends_on for k in later} == {
            earlier[0].capture_id
        }
    for index in range(len(revisions) - 1):
        # The last part of this revision is not received: the next one must wait.
        held = revisions[index][-1].capture_id
        later = {k.capture_id for rev in revisions[index + 1 :] for k in rev}
        client, asked = _courier({held})
        box.deliver(client)
        assert not later & set(asked), f"{shape} released before {held}"
        assert box.get(held).state == "pending"
        # Reset for the next round: a fresh outbox with the same chain.
        box = Outbox(
            tmp_path / f"outbox{index}.db", "one", OutboxLimits(min_free_bytes=1)
        )
        revisions = _chain(box, shape)
    client, asked = _courier(set())
    box.deliver(client)
    assert {e.state for e in box.entries()} == {"received"}


def test_correction_is_released_only_when_all_parts_of_the_predecessor_are_received(
    tmp_path: Path,
) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    zero, one = _chain(box, [2, 1])
    client, asked = _courier({zero[1].capture_id})
    box.deliver(client)
    assert box.get(zero[0].capture_id).state == "received"
    assert one[0].capture_id not in asked  # part 0 alone is not enough
    client, asked = _courier(set())
    box.deliver(client)
    assert box.get(one[0].capture_id).state == "received"


def test_quarantined_part_of_the_previous_revision_blocks_the_correction(
    tmp_path: Path,
) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    zero, one = _chain(box, [2, 2])
    quarantined = zero[1].capture_id
    client, asked = _courier(set(), {quarantined})
    box.deliver(client)
    assert box.get(quarantined).state == "quarantine"
    assert box.get(zero[0].capture_id).state == "received"
    assert not {k.capture_id for k in one} & set(asked)
    box.deliver(client)  # still blocked on later passes
    assert not {k.capture_id for k in one} & set(asked)
    # After the operator retries and Courier accepts it, the correction follows.
    box.retry(quarantined)
    client, asked = _courier(set())
    box.deliver(client)
    assert {e.state for e in box.entries()} == {"received"}


def test_correction_close_requires_the_whole_previous_revision_to_be_frozen(
    tmp_path: Path,
) -> None:
    box = Outbox(tmp_path / "outbox.db", "one", OutboxLimits(min_free_bytes=1))
    keys = [BundleKey("one", "a", "2026-11-20", n, 0) for n in range(2)]
    for key in keys:
        box.buffer(key, b"body")
    box.close(keys[0], manifest(keys[0], b"body"))  # part 1 is still open
    correction = BundleKey("one", "a", "2026-11-20", 0, 1)
    box.buffer(correction, b"body")
    with pytest.raises(ValueError, match="invalid_correction_dependency"):
        box.close(
            correction, manifest(correction, b"body"), depends_on=keys[0].capture_id
        )
