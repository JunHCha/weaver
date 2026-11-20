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
