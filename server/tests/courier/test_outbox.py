import hashlib
import logging
import os

import pytest

from weaver.courier import Courier

from .conftest import BODY, CAP, CAP2, assert_schema_valid, manifest_for, write_json


def publish_capture(outbox, capture_id=CAP, body=BODY, ext="txt", **overrides):
    """Shepherd order: body first, manifest last."""
    (outbox / f"{capture_id}.{ext}").write_bytes(body)
    manifest = manifest_for(capture_id, body, ext, **overrides)
    write_json(outbox / f"{capture_id}.json", manifest)
    return manifest


def test_fixture_matches_shared_schema():
    assert_schema_valid("Manifest", manifest_for())


def test_verified_capture_is_handed_over(courier, paths, captures):
    publish_capture(paths.outbox)
    report = courier.scan_once()
    assert report.accepted == [CAP]
    assert report.problems == {}
    (capture,) = captures.accepted
    assert capture.manifest.capture_id == CAP
    assert capture.payload_path == (paths.outbox / f"{CAP}.txt").resolve()
    assert capture.manifest_path == paths.outbox / f"{CAP}.json"


def test_missing_body_waits_then_late_arrival_is_processed(courier, paths, captures):
    write_json(paths.outbox / f"{CAP}.json", manifest_for())
    report = courier.scan_once()
    assert report.problems == {f"outbox/{CAP}.json": "payload_missing"}
    assert captures.accepted == []

    (paths.outbox / f"{CAP}.txt").write_bytes(BODY[:5])  # partially synced
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "size_mismatch"}

    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    assert courier.scan_once().accepted == [CAP]


def test_body_without_manifest_is_not_processed(courier, paths, captures):
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    report = courier.scan_once()
    assert report.accepted == [] and report.problems == {}


@pytest.mark.parametrize("field", ["size", "sha256"])
def test_size_or_hash_mismatch_is_never_handed_over(
    courier, paths, captures, field, caplog
):
    bad = {"size": len(BODY) + 1} if field == "size" else {"sha256": "0" * 64}
    publish_capture(paths.outbox, **bad)
    caplog.set_level(logging.DEBUG, "weaver.courier")
    for _ in range(3):  # retried on every rescan, never accepted
        report = courier.scan_once()
        assert report.problems == {f"outbox/{CAP}.json": f"{field}_mismatch"}
    assert captures.accepted == [] and captures.stored == {}
    assert list(paths.receipts.iterdir()) == []
    # First occurrence at INFO, repeats demoted to DEBUG; never file contents.
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.INFO, logging.DEBUG, logging.DEBUG]
    text = caplog.text
    assert "점검" not in text and BODY.decode() not in text


def test_hash_mismatch_with_equal_size(courier, paths, captures):
    tampered = BODY[:-1] + b"!"
    (paths.outbox / f"{CAP}.txt").write_bytes(tampered)
    write_json(paths.outbox / f"{CAP}.json", manifest_for())
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "sha256_mismatch"}
    assert captures.accepted == []


@pytest.mark.parametrize(
    "content",
    [b'{"schema_version": 1, "capture_id": "cap_', b"", b"\xff\xfe", b"[1]"],
    ids=["partial", "empty", "binary", "not-object"],
)
def test_invalid_json_is_rejected(courier, paths, captures, content):
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    (paths.outbox / f"{CAP}.json").write_bytes(content)
    code = courier.scan_once().problems[f"outbox/{CAP}.json"]
    assert code in {"invalid_json", "invalid_schema"}
    assert captures.accepted == []


def test_unknown_version_is_logged_not_processed(courier, paths, captures, caplog):
    publish_capture(paths.outbox, schema_version=2)
    caplog.set_level(logging.WARNING, "weaver.courier")
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "unknown_version"}
    assert captures.accepted == []
    assert [r.getMessage() for r in caplog.records] == [
        f"courier outbox/{CAP}.json: unknown_version"
    ]


@pytest.mark.parametrize("version", [1.0, True, "1"])
def test_non_integer_version_token_is_invalid(courier, paths, captures, version):
    publish_capture(paths.outbox, schema_version=version)
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "invalid_schema"}


@pytest.mark.parametrize(
    "name",
    [
        f".syncthing.{CAP}.json.tmp",
        f"~syncthing~{CAP}.json.tmp",
        f".{CAP}.json",
        f".{CAP}.json.0123456789abcdef.tmp",
    ],
)
def test_temporary_and_dot_files_are_ignored(courier, paths, captures, name):
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    write_json(paths.outbox / name, manifest_for())
    report = courier.scan_once()
    assert report.accepted == [] and report.problems == {}


def test_sync_conflict_copy_is_reported_not_processed(courier, paths, captures):
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    name = f"{CAP}.sync-conflict-20261107-153200-ABCDEFG.json"
    write_json(paths.outbox / name, manifest_for())
    assert courier.scan_once().problems == {f"outbox/{name}": "sync_conflict"}
    assert captures.accepted == []


def test_forged_capture_id_is_rejected(courier, paths, captures):
    (paths.outbox / f"{CAP2}.txt").write_bytes(BODY)
    write_json(paths.outbox / f"{CAP}.json", manifest_for(CAP2))
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "id_mismatch"}
    assert captures.accepted == []


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("/etc/passwd", "invalid_schema"),
        ("../secret.txt", "invalid_schema"),
        (f"sub/{CAP}.txt", "invalid_payload"),
        (f"{CAP2}.txt", "invalid_payload"),
        (CAP, "invalid_payload"),
        (f"{CAP}.json", "invalid_payload"),
    ],
)
def test_payload_path_escape_is_rejected(courier, paths, captures, payload, code):
    (paths.outbox / f"{CAP2}.txt").write_bytes(BODY)
    publish_capture(paths.outbox, payload=payload)
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": code}
    assert captures.accepted == []


def test_external_symlink_payload_is_rejected(courier, paths, captures, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(BODY)
    os.symlink(outside, paths.outbox / f"{CAP}.txt")
    write_json(paths.outbox / f"{CAP}.json", manifest_for())
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "payload_outside"}
    assert captures.accepted == []


def test_symlinked_manifest_is_rejected(courier, paths, captures, tmp_path):
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    outside = write_json(tmp_path / "manifest.json", manifest_for())
    os.symlink(outside, paths.outbox / f"{CAP}.json")
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "symlink"}


def test_restart_rescan_recovers_and_does_not_duplicate(
    paths, captures, revisions, decisions
):
    first = Courier(paths, captures, revisions, decisions)
    write_json(paths.outbox / f"{CAP}.json", manifest_for())
    assert first.scan_once().accepted == []  # body not synced before shutdown
    # Server is down while the body and a second capture arrive.
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    other = b"second synthetic capture"
    publish_capture(paths.outbox, CAP2, other, "m4a", kind="fleece", channel="call")

    restarted = Courier(paths, captures, revisions, decisions)
    assert restarted.scan_once().accepted == [CAP, CAP2]
    # Same files reappear on later scans and after another restart.
    assert restarted.scan_once().duplicates == [CAP, CAP2]
    again = Courier(paths, captures, revisions, decisions)
    assert again.scan_once().accepted == []
    assert len(captures.accepted) == 2


def test_same_body_under_new_capture_id_is_duplicate(courier, paths, captures):
    publish_capture(paths.outbox)
    courier.scan_once()
    publish_capture(paths.outbox, CAP2)
    report = courier.scan_once()
    assert report.duplicates == [CAP, CAP2] and report.accepted == []
    assert len(captures.accepted) == 1


def test_accept_failure_is_retried(courier, paths, captures):
    publish_capture(paths.outbox)
    captures.failures = 1
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "accept_failed"}
    assert (paths.outbox / f"{CAP}.json").exists()
    assert courier.scan_once().accepted == [CAP]


def test_large_body_hash_streams(courier, paths, captures):
    body = os.urandom(3 * (1 << 20) + 7)
    publish_capture(paths.outbox, body=body, ext="m4a", kind="fleece")
    assert courier.scan_once().accepted == [CAP]
    assert captures.accepted[0].manifest.sha256 == hashlib.sha256(body).hexdigest()
