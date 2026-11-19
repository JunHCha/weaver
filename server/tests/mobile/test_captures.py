"""capture upload: staging, manifest, Receipt, retries and lost responses."""

import hashlib
import time

from weaver.mobile.captures import CaptureService
from weaver.schema import Receipt

from .helpers import BODY, CAP, CAP2, Harness, manifest, put_manifest, upload

BASE = "/mobile/v1/captures"


def receipt_of(h, headers, capture_id=CAP):
    return h.client.get(f"{BASE}/{capture_id}/receipt", headers=headers)


def staging_files(h):
    return list((h.config.state_root / "mobile" / "staging").iterdir())


def test_payload_then_manifest_then_receipt(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    assert receipt_of(h, headers).status_code == 404  # not received yet
    staged = upload(h, headers)
    assert (staged.status_code, staged.json()["state"]) == (201, "staged")
    # A staged body is not a Receipt.
    assert receipt_of(h, headers).status_code == 404
    assert h.archive.stored_capture(CAP) is None
    stored = put_manifest(h, headers)
    assert stored.status_code == 201
    receipt = Receipt.model_validate_json(stored.text)
    assert receipt.capture_id == CAP
    assert receipt.sha256 == hashlib.sha256(BODY).hexdigest()
    assert receipt_of(h, headers).json() == stored.json()
    # The archive holds the original; staging is released.
    source = h.archive.get(receipt.source_id)
    assert (h.config.archive_root / source.payload_ref).read_bytes() == BODY
    assert staging_files(h) == []
    assert h.store.staged(CAP) is None


def test_retries_are_idempotent_and_return_the_original_receipt(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    assert upload(h, headers).status_code == 201
    assert upload(h, headers).status_code == 200  # same bytes again
    first = put_manifest(h, headers)
    again = put_manifest(h, headers)  # the 201 response was lost
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()  # original received_at
    # Payload re-sent after the manifest committed: no new staging, no change.
    after = upload(h, headers)
    assert (after.status_code, after.json()["state"]) == (200, "stored")
    assert staging_files(h) == []


def test_lost_manifest_response_is_recovered_by_receipt_get(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    upload(h, headers)
    put_manifest(h, headers)  # committed, but the phone never saw the response
    lookup = receipt_of(h, headers)
    assert lookup.status_code == 200 and lookup.json()["capture_id"] == CAP


def test_manifest_before_payload_is_a_retryable_conflict(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    early = put_manifest(h, headers)
    assert (early.status_code, early.json()) == (
        409,
        {"code": "payload_missing", "retryable": True},
    )
    assert h.archive.stored_capture(CAP) is None
    upload(h, headers)
    assert put_manifest(h, headers).status_code == 201


def test_payload_validation_hash_length_size(tmp_path):
    h = Harness(tmp_path, "max_payload_bytes = 1024\n")
    headers = h.pair()
    url = f"{BASE}/{CAP}/payload"
    ok_hash = hashlib.sha256(BODY).hexdigest()
    bad = h.client.put(
        url, content=BODY, headers={**headers, "X-Content-SHA256": "0" * 64}
    )
    assert (bad.status_code, bad.json()["code"]) == (422, "sha256_mismatch")
    missing = h.client.put(url, content=BODY, headers=headers)
    assert missing.json()["code"] == "invalid_sha256"
    short = h.client.put(
        url, content=BODY, headers={**headers, "X-Content-SHA256": "ZZ"}
    )
    assert short.status_code == 422
    big = b"x" * 2048
    too_big = h.client.put(
        url,
        content=big,
        headers={**headers, "X-Content-SHA256": hashlib.sha256(big).hexdigest()},
    )
    assert (too_big.status_code, too_big.json()["code"]) == (413, "too_large")
    bad_id = h.client.put(
        f"{BASE}/not-a-capture/payload",
        content=BODY,
        headers={**headers, "X-Content-SHA256": ok_hash},
    )
    assert bad_id.status_code == 422
    # Nothing was staged by the failed attempts, and no temporaries remain.
    assert staging_files(h) == [] and h.store.staged(CAP) is None


def test_truncated_upload_is_not_staged(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()

    def chunks():
        yield BODY[:5]
        raise RuntimeError("connection reset")

    try:
        h.client.put(
            f"{BASE}/{CAP}/payload",
            content=chunks(),
            headers={
                **headers,
                "X-Content-SHA256": hashlib.sha256(BODY).hexdigest(),
                "Content-Length": str(len(BODY)),
            },
        )
    except RuntimeError:
        pass
    assert h.store.staged(CAP) is None and staging_files(h) == []
    assert upload(h, headers).status_code == 201  # the retry starts clean


def test_same_capture_id_with_other_content_conflicts(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    upload(h, headers)
    other = b"other bytes"
    conflict = upload(h, headers, body=other)
    assert (conflict.status_code, conflict.json()["code"]) == (409, "payload_conflict")
    put_manifest(h, headers)
    after = upload(h, headers, body=other)
    assert (after.status_code, after.json()["code"]) == (409, "capture_conflict")
    assert put_manifest(h, headers, manifest(body=other)).json()["code"] == (
        "capture_conflict"
    )
    # Different metadata for the same bytes is also a conflict; original kept.
    changed = manifest(captured_at="2026-11-08T00:00:00Z")
    assert put_manifest(h, headers, changed).json()["code"] == "manifest_conflict"
    assert receipt_of(h, headers).status_code == 200


def test_other_capture_with_same_hash_links_to_the_same_source(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    upload(h, headers)
    first = put_manifest(h, headers)
    upload(h, headers, CAP2)
    second = put_manifest(h, headers, manifest(CAP2))
    assert second.status_code == 201
    assert second.json()["source_id"] == first.json()["source_id"]
    assert second.json()["capture_id"] == CAP2
    # The body is archived, so even without staging a third id is accepted.
    third = put_manifest(h, headers, manifest("cap_01ARZ3NDEKTSV4RRFFQ69G5FA2"))
    assert third.status_code == 201


def test_manifest_validation(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    upload(h, headers)
    url = f"{BASE}/{CAP}/manifest"

    def code(content):
        return h.client.put(url, content=content, headers=headers).json()["code"]

    assert code(b"{") == "invalid_manifest"
    assert code(manifest(CAP2).model_dump_json()) == "id_mismatch"
    assert code(manifest(payload="other.txt").model_dump_json()) == "invalid_payload"
    assert code(manifest(size=1).model_dump_json()) == "payload_mismatch"
    assert h.client.put(url, content=b" " * 70000, headers=headers).status_code == 413


def test_http_and_files_share_one_capture_journal(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    # The file path stores the capture first (late Syncthing arrival)...
    outbox = h.config.courier.outbox
    (outbox / manifest().payload).write_bytes(BODY)
    (outbox / f"{CAP}.json").write_text(manifest().model_dump_json())
    assert h.courier.scan_once().accepted == [CAP]
    # ...and a later HTTP retry of the same capture gets the same receipt.
    stored = h.archive.stored_capture(CAP)
    assert stored is not None
    upload(h, headers)
    replay = put_manifest(h, headers)
    assert replay.status_code == 200
    assert Receipt.model_validate_json(replay.text) == stored[1]
    # In the other direction the file scan sees the HTTP-stored capture as done.
    upload(h, headers, CAP2)
    put_manifest(h, headers, manifest(CAP2))
    (outbox / manifest(CAP2).payload).write_bytes(BODY)
    (outbox / f"{CAP2}.json").write_text(manifest(CAP2).model_dump_json())
    assert sorted(h.courier.scan_once().duplicates) == [CAP, CAP2]


def test_unfinished_staging_expires_but_committed_archives_do_not(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    upload(h, headers)  # never gets a manifest
    service = CaptureService(
        h.archive,
        h.store,
        h.config.state_root / "mobile" / "staging",
        max_payload_bytes=10**6,
        staging_days=1,
    )
    assert service.collect_garbage() == []
    assert service.collect_garbage(now=time.time() + 3 * 86400) == [CAP]
    assert h.store.staged(CAP) is None
    assert upload(h, headers).status_code == 201  # the phone can upload again
    put_manifest(h, headers)
    assert service.collect_garbage(now=time.time() + 30 * 86400) == []
    assert h.archive.stored_capture(CAP) is not None
