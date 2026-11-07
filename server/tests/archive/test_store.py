"""SourceStore contract (#18): storage, dedup, conflicts and receipts."""

import errno
import hashlib
import json
import os
import stat
import threading

import pytest

from weaver.archive import (
    ArchiveIntegrityError,
    PayloadMismatch,
    SourceNotFound,
)
from weaver.archive import store as store_module
from weaver.courier import CaptureConflict, VerifiedCapture
from weaver.schema import Receipt, Source

from .conftest import AUDIO, CAP, CAP2, CAP3, TEXT, manifest, outbox_file


def receipt_of(dirs, capture_id):
    path = dirs["receipts"] / f"{capture_id}.json"
    return Receipt.model_validate_json(path.read_bytes())


def all_files(root):
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def payload_files(dirs):
    return [p for p in all_files(dirs["archive"]) if p.startswith("payloads/")]


# ---- new put / get -----------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "ext", "kind", "channel"),
    [
        (TEXT, "txt", "text", "share_text"),
        (TEXT, "txt", "text", "kakao"),
        (AUDIO, "m4a", "fleece", "call"),
        (AUDIO, "m4a", "fleece", "voice_memo"),
    ],
)
def test_new_capture_is_archived_indexed_and_receipted(
    store, dirs, body, ext, kind, channel
):
    m = manifest(body=body, ext=ext, kind=kind, channel=channel)
    source = store.put(m, outbox_file(dirs["outbox"], m, body))

    assert source.source_id.startswith("src_")
    assert (source.capture_id, source.kind, source.channel) == (CAP, kind, channel)
    assert source.sha256 == m.sha256 and source.hints == m.hints
    assert source.payload_ref == f"payloads/{m.sha256[:2]}/{m.sha256}.{ext}"
    archived = dirs["archive"] / source.payload_ref
    assert archived.read_bytes() == body
    assert stat.S_IMODE(archived.stat().st_mode) == 0o600

    receipt = receipt_of(dirs, CAP)
    assert (receipt.capture_id, receipt.source_id, receipt.sha256) == (
        CAP,
        source.source_id,
        m.sha256,
    )
    assert json.loads((dirs["receipts"] / f"{CAP}.json").read_text()).keys() == {
        "schema_version",
        "capture_id",
        "source_id",
        "sha256",
        "received_at",
    }
    assert store.get(source.source_id) == source
    assert store.source_for_capture(CAP) == source.source_id


def test_get_unknown_source_raises(store):
    with pytest.raises(SourceNotFound):
        store.get("src_01ARZ3NDEKTSV4RRFFQ69G5FA0")
    with pytest.raises(SourceNotFound):
        store.locate("src_01ARZ3NDEKTSV4RRFFQ69G5FA0")
    with pytest.raises(SourceNotFound):
        store.captures_for("src_01ARZ3NDEKTSV4RRFFQ69G5FA0")
    assert store.source_for_capture(CAP) is None


def test_get_reads_archive_after_outbox_copy_is_removed(open_store, dirs):
    m = manifest()
    payload = outbox_file(dirs["outbox"], m, TEXT)
    source = open_store().put(m, payload)
    payload.unlink()  # phone cleaned up after the receipt
    (dirs["outbox"] / f"{CAP}.json").unlink(missing_ok=True)

    restarted = open_store()
    assert restarted.get(source.source_id) == source
    located = restarted.locate(source.source_id)
    assert located.source == source
    assert located.payload_path.read_bytes() == TEXT
    assert located.payload_path.is_relative_to(dirs["archive"])


# ---- duplicates and conflicts ------------------------------------------------


def test_same_capture_resent_reuses_source_and_receipt(store, dirs):
    m = manifest()
    first = store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    receipt = (dirs["receipts"] / f"{CAP}.json").read_bytes()
    again = store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    assert again == first
    assert (dirs["receipts"] / f"{CAP}.json").read_bytes() == receipt
    assert len(payload_files(dirs)) == 1
    assert store.status(CAP, m.sha256) == "stored"


def test_different_capture_same_hash_links_existing_source(store, dirs):
    m1 = manifest(CAP, channel="share_text", hints={"counterpart": None})
    m2 = manifest(
        CAP2,
        channel="kakao",
        hints={"counterpart": "합성 상대"},
        occurred_at="2026-11-07T15:20:00+09:00",
    )
    first = store.put(m1, outbox_file(dirs["outbox"], m1, TEXT))
    assert store.status(CAP2, m2.sha256) == "new"
    second = store.put(m2, outbox_file(dirs["outbox"], m2, TEXT))

    # Source is fixed to the first manifest; no new Source or payload.
    assert second == first and second.channel == "share_text"
    assert len(store.sources()) == 1 and len(payload_files(dirs)) == 1
    assert receipt_of(dirs, CAP2).source_id == first.source_id
    assert receipt_of(dirs, CAP2).capture_id == CAP2
    assert store.source_for_capture(CAP2) == first.source_id
    # Every manifest is preserved verbatim, in arrival order.
    records = store.captures_for(first.source_id)
    assert [r.manifest for r in records] == [m1, m2]
    assert [r.receipt.capture_id for r in records] == [CAP, CAP2]


def test_same_capture_different_hash_is_conflict(store, dirs):
    m = manifest()
    source = store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    archived_before = all_files(dirs["archive"])
    receipt = (dirs["receipts"] / f"{CAP}.json").read_bytes()

    other = b"different synthetic body"
    changed = manifest(body=other)
    assert store.status(CAP, changed.sha256) == "conflict"
    with pytest.raises(CaptureConflict):
        store.put(changed, outbox_file(dirs["outbox"], changed, other))

    assert (dirs["receipts"] / f"{CAP}.json").read_bytes() == receipt
    assert (dirs["archive"] / source.payload_ref).read_bytes() == TEXT
    assert payload_files(dirs) == [
        p for p in archived_before if p.startswith("payloads/")
    ]
    assert store.get(source.source_id) == source
    assert len(store.sources()) == 1


def test_restart_resolves_every_capture_without_duplicates(open_store, dirs):
    m1, m2 = manifest(CAP), manifest(CAP2)
    m3 = manifest(CAP3, body=AUDIO, ext="m4a", kind="fleece", channel="call")
    first = open_store()
    s1 = first.put(m1, outbox_file(dirs["outbox"], m1, TEXT))
    first.put(m2, outbox_file(dirs["outbox"], m2, TEXT))
    s3 = first.put(m3, outbox_file(dirs["outbox"], m3, AUDIO))

    restarted = open_store()
    assert {c: restarted.source_for_capture(c) for c in (CAP, CAP2, CAP3)} == {
        CAP: s1.source_id,
        CAP2: s1.source_id,
        CAP3: s3.source_id,
    }
    for m, body in ((m1, TEXT), (m2, TEXT), (m3, AUDIO)):
        restarted.put(m, outbox_file(dirs["outbox"], m, body))
    assert [s.source_id for s in restarted.sources()] == [s1.source_id, s3.source_id]
    assert len(payload_files(dirs)) == 2


def test_concurrent_puts_converge_to_one_source(open_store, dirs):
    manifests = [manifest(CAP), manifest(CAP), manifest(CAP2), manifest(CAP3)] * 4
    barrier = threading.Barrier(len(manifests))
    results, errors = [], []

    def worker(m):
        try:
            # Separate instances share only the files, as separate processes do.
            store = open_store()
            barrier.wait()
            results.append(store.put(m, dirs["outbox"] / f"{CAP}.txt"))
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    (dirs["outbox"] / f"{CAP}.txt").write_bytes(TEXT)
    threads = [threading.Thread(target=worker, args=(m,)) for m in manifests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len({r.source_id for r in results}) == 1
    store = open_store()
    assert len(store.sources()) == 1 and len(payload_files(dirs)) == 1
    assert len(store.captures_for(results[0].source_id)) == 3
    assert {receipt_of(dirs, c).source_id for c in (CAP, CAP2, CAP3)} == {
        results[0].source_id
    }


# ---- failures leave no success receipt --------------------------------------


def test_disk_full_during_copy_leaves_no_trace(store, dirs, monkeypatch):
    existing = manifest(CAP2, body=AUDIO, ext="m4a", kind="fleece")
    kept = store.put(existing, outbox_file(dirs["outbox"], existing, AUDIO))
    before = all_files(dirs["archive"])
    receipts_before = sorted(p.name for p in dirs["receipts"].iterdir())

    def full(fd, chunk):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store_module, "_write_chunk", full)
    m = manifest()
    with pytest.raises(OSError):
        store.put(m, outbox_file(dirs["outbox"], m, TEXT))

    assert sorted(p.name for p in dirs["receipts"].iterdir()) == receipts_before
    assert payload_files(dirs) == [p for p in before if p.startswith("payloads/")]
    assert store.status(CAP, m.sha256) == "new"
    assert store.get(kept.source_id) == kept
    assert (dirs["archive"] / kept.payload_ref).read_bytes() == AUDIO

    monkeypatch.undo()
    assert store.put(m, dirs["outbox"] / m.payload).capture_id == CAP


def test_unreadable_payload_leaves_no_trace(store, dirs):
    m = manifest()
    with pytest.raises(OSError):
        store.put(m, dirs["outbox"] / m.payload)  # body never arrived
    assert payload_files(dirs) == [] and list(dirs["receipts"].iterdir()) == []


@pytest.mark.parametrize(
    "body",
    [TEXT[:-1] + b"!", TEXT[:-3], TEXT + b"extra"],
    ids=["hash", "short", "long"],
)
def test_payload_changed_after_verification_is_rejected(store, dirs, body):
    m = manifest()
    with pytest.raises(PayloadMismatch):
        store.put(m, outbox_file(dirs["outbox"], m, body))
    assert payload_files(dirs) == [] and list(dirs["receipts"].iterdir()) == []
    assert store.status(CAP, m.sha256) == "new"


def test_symlinked_payload_is_not_followed(store, dirs, tmp_path):
    m = manifest()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(TEXT)
    os.symlink(outside, dirs["outbox"] / m.payload)
    with pytest.raises(PayloadMismatch):
        store.put(m, dirs["outbox"] / m.payload)
    assert list(dirs["receipts"].iterdir()) == []


def test_foreign_file_at_archive_path_is_never_overwritten(store, dirs):
    m = manifest()
    final = dirs["archive"] / "payloads" / m.sha256[:2] / f"{m.sha256}.txt"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"corrupt")
    with pytest.raises(ArchiveIntegrityError):
        store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    assert final.read_bytes() == b"corrupt"
    assert list(dirs["receipts"].iterdir()) == []


# ---- interrupted stages recover on retry ------------------------------------


class Crash(BaseException):
    """Simulated power loss: not an Exception, so nothing may swallow it."""


def crash_after(monkeypatch, store, name):
    original = getattr(store, name)

    def wrapper(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(store, name, wrapper)


def test_crash_after_payload_before_index_recovers(open_store, dirs, monkeypatch):
    m = manifest()
    payload = outbox_file(dirs["outbox"], m, TEXT)
    store = open_store()
    crash_after(monkeypatch, store, "_store_payload")
    with pytest.raises(Crash):
        store.put(m, payload)
    assert len(payload_files(dirs)) == 1  # orphan body, but no index, no receipt
    assert list(dirs["receipts"].iterdir()) == []

    retried = open_store()
    assert retried.status(CAP, m.sha256) == "new"
    source = retried.put(m, payload)
    assert len(payload_files(dirs)) == 1
    assert receipt_of(dirs, CAP).source_id == source.source_id


def test_crash_after_index_before_receipt_is_reissued(open_store, dirs, monkeypatch):
    m = manifest()
    store = open_store()
    crash_after(monkeypatch, store, "_index_capture")
    with pytest.raises(Crash):
        store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    assert list(dirs["receipts"].iterdir()) == []

    retried = open_store()
    assert retried.status(CAP, m.sha256) == "stored"
    retried.confirm(CAP)
    receipt = receipt_of(dirs, CAP)
    assert receipt.source_id == retried.source_for_capture(CAP)
    # Reissue is byte-identical and idempotent.
    data = (dirs["receipts"] / f"{CAP}.json").read_bytes()
    retried.confirm(CAP)
    assert (dirs["receipts"] / f"{CAP}.json").read_bytes() == data


def test_crash_while_writing_receipt_recovers(open_store, dirs, monkeypatch):
    m = manifest()
    store = open_store()

    def torn(final, data):
        (final.parent / f".{final.name}.0123456789abcdef.tmp").write_bytes(data[:7])
        raise Crash()

    monkeypatch.setattr(store_module, "write_bytes_atomic", torn)
    with pytest.raises(Crash):
        store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    monkeypatch.undo()
    assert not (dirs["receipts"] / f"{CAP}.json").exists()

    retried = open_store()
    retried.put(m, dirs["outbox"] / m.payload)
    assert receipt_of(dirs, CAP).capture_id == CAP


def test_corrupted_receipt_is_rewritten_by_confirm(store, dirs):
    m = manifest()
    store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    good = (dirs["receipts"] / f"{CAP}.json").read_bytes()
    (dirs["receipts"] / f"{CAP}.json").write_bytes(b"{}")
    store.confirm(CAP)
    assert (dirs["receipts"] / f"{CAP}.json").read_bytes() == good


def test_confirm_unknown_capture_raises(store):
    with pytest.raises(SourceNotFound):
        store.confirm(CAP)


def test_stale_archive_temporaries_are_swept(open_store, dirs):
    m = manifest()
    directory = dirs["archive"] / "payloads" / m.sha256[:2]
    directory.mkdir(parents=True)
    stale = directory / f".{m.sha256}.0123456789abcdef.tmp"
    fresh = directory / f".{m.sha256}.fedcba9876543210.tmp"
    foreign = directory / "keep.me"
    for path in (stale, fresh, foreign):
        path.write_bytes(b"x")
    os.utime(stale, (0, 0))
    open_store()
    assert not stale.exists() and fresh.exists() and foreign.exists()


# ---- CaptureStore adapter ----------------------------------------------------


def test_accept_puts_verified_capture(store, dirs):
    m = manifest()
    payload = outbox_file(dirs["outbox"], m, TEXT)
    store.accept(VerifiedCapture(m, dirs["outbox"] / f"{CAP}.json", payload))
    assert store.status(CAP, m.sha256) == "stored"
    assert receipt_of(dirs, CAP).sha256 == hashlib.sha256(TEXT).hexdigest()


def test_index_and_directories_are_private(store, dirs):
    m = manifest()
    store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    index = dirs["archive"] / "index.sqlite3"
    assert stat.S_IMODE(index.stat().st_mode) == 0o600
    payloads = dirs["archive"] / "payloads"
    for directory in (payloads, next(payloads.iterdir())):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_unknown_index_version_is_refused(open_store, dirs):
    import sqlite3

    open_store()
    with sqlite3.connect(dirs["archive"] / "index.sqlite3") as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(store_module.ArchiveError):
        open_store()


def test_source_json_matches_shared_schema(store, dirs):
    from jsonschema import Draft202012Validator, FormatChecker

    from .conftest import ROOT

    m = manifest()
    source = store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    for name, model in (("Source", source), ("Receipt", receipt_of(dirs, CAP))):
        schema = json.loads((ROOT / "schema" / f"{name}.schema.json").read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(
            json.loads(model.model_dump_json())
        )
    assert isinstance(source, Source)


# ---- PR #44 review regressions ----------------------------------------------


def record_fsyncs(monkeypatch):
    calls = []
    real = store_module._fsync_directory

    def recording(directory):
        calls.append(directory)
        real(directory)

    monkeypatch.setattr(store_module, "_fsync_directory", recording)
    return calls


def test_reused_payload_directory_is_fsynced_before_index(
    open_store, dirs, monkeypatch
):
    """Crash after link but before the directory fsync: the retry must make
    the entry durable before committing the index and publishing a receipt."""
    m = manifest()
    final = dirs["archive"] / "payloads" / m.sha256[:2] / f"{m.sha256}.txt"
    final.parent.mkdir(parents=True)
    final.write_bytes(TEXT)
    store = open_store()
    calls = record_fsyncs(monkeypatch)
    original = store._index_capture

    def index(*args):
        assert final.parent in calls and final.parent.parent in calls
        return original(*args)

    monkeypatch.setattr(store, "_index_capture", index)
    store.put(m, outbox_file(dirs["outbox"], m, TEXT))


def test_existing_shard_directory_still_fsyncs_parent(store, dirs, monkeypatch):
    m = manifest()
    (dirs["archive"] / "payloads" / m.sha256[:2]).mkdir(parents=True)
    calls = record_fsyncs(monkeypatch)
    store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    shard = dirs["archive"] / "payloads" / m.sha256[:2]
    assert shard in calls and shard.parent in calls


def test_index_connection_requests_full_fsync(store):
    with store._read() as conn:
        assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
        assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_payload_body_uses_full_fsync(store, dirs, monkeypatch):
    synced = []
    real = store_module.full_fsync
    monkeypatch.setattr(
        store_module, "full_fsync", lambda fd: (synced.append(fd), real(fd))
    )
    m = manifest()
    store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    assert synced


def test_private_modes_are_corrected_on_every_start(open_store, dirs):
    store = open_store()
    m = manifest()
    store.put(m, outbox_file(dirs["outbox"], m, TEXT))
    index = dirs["archive"] / "index.sqlite3"
    os.chmod(index, 0o644)
    os.chmod(dirs["archive"], 0o755)
    open_store()
    assert stat.S_IMODE(index.stat().st_mode) == 0o600
    assert stat.S_IMODE(dirs["archive"].stat().st_mode) == 0o700
    for suffix in ("-wal", "-shm"):
        sidecar = dirs["archive"] / f"index.sqlite3{suffix}"
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_index_is_created_private_before_sqlite_opens_it(dirs, open_store):
    old = os.umask(0)
    try:
        open_store()
    finally:
        os.umask(old)
    index = dirs["archive"] / "index.sqlite3"
    assert stat.S_IMODE(index.stat().st_mode) == 0o600


def test_rollback_failure_does_not_mask_original_error(store):
    class Original(Exception):
        pass

    with pytest.raises(Original):
        with store._write() as conn:
            conn.execute("ROLLBACK")  # SQLite already rolled back (e.g. SQLITE_FULL)
            raise Original()
