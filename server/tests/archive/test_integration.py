"""ArchiveStore wired to the real Courier, real process crashes and config."""

import subprocess
import sys
import textwrap

from weaver.archive import ArchiveStore
from weaver.config import CourierPaths, load_config
from weaver.courier import Courier
from weaver.schema import Receipt

from .conftest import CAP, CAP2, TEXT, manifest


class NoRevisions:
    def latest_rev(self, proposal_id):
        return None


class NoDecisions:
    def is_applied(self, proposal_id, rev):
        return False

    def apply(self, verdict):  # pragma: no cover - no decisions in these tests
        raise AssertionError


def courier_paths(tmp_path):
    sync = tmp_path / "sync"
    folders = {n: sync / n for n in ("outbox", "receipts", "proposals", "decisions")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    return CourierPaths(**folders)


def shepherd(outbox, m, body):
    (outbox / m.payload).write_bytes(body)
    (outbox / f"{m.capture_id}.json").write_text(m.model_dump_json())


def receipt(paths, capture_id):
    return Receipt.model_validate_json(
        (paths.receipts / f"{capture_id}.json").read_bytes()
    )


def test_courier_round_trip_with_archive(tmp_path):
    paths = courier_paths(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    store = ArchiveStore(archive, paths.receipts)
    courier = Courier(paths, store, NoRevisions(), NoDecisions())

    m1, m2 = manifest(CAP), manifest(CAP2, channel="kakao")
    shepherd(paths.outbox, m1, TEXT)
    assert courier.scan_once().accepted == [CAP]
    source_id = receipt(paths, CAP).source_id
    assert receipt(paths, CAP).sha256 == m1.sha256

    # Same body under a new capture: linked, with its own receipt.
    shepherd(paths.outbox, m2, TEXT)
    report = courier.scan_once()
    assert report.accepted == [CAP2] and report.duplicates == [CAP]
    assert receipt(paths, CAP2).source_id == source_id

    # Receipt lost while the manifest is still in outbox: re-published.
    (paths.receipts / f"{CAP}.json").unlink()
    assert courier.scan_once().duplicates == [CAP, CAP2]
    assert receipt(paths, CAP).source_id == source_id

    # Phone cleans up outbox (body first); the archive still serves the source.
    for capture in (CAP, CAP2):
        (paths.outbox / f"{capture}.txt").unlink()
    assert courier.scan_once().problems == {}
    for capture in (CAP, CAP2):
        (paths.outbox / f"{capture}.json").unlink()
    assert store.locate(source_id).payload_path.read_bytes() == TEXT

    # Once the manifest is gone a cleaned receipt is not resurrected.
    (paths.receipts / f"{CAP2}.json").unlink()
    courier.scan_once()
    assert not (paths.receipts / f"{CAP2}.json").exists()


def test_courier_reports_conflict_and_keeps_original(tmp_path):
    paths = courier_paths(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    store = ArchiveStore(archive, paths.receipts)
    courier = Courier(paths, store, NoRevisions(), NoDecisions())
    shepherd(paths.outbox, manifest(), TEXT)
    courier.scan_once()
    before = (paths.receipts / f"{CAP}.json").read_bytes()

    other = b"different synthetic body"
    shepherd(paths.outbox, manifest(body=other), other)
    assert courier.scan_once().problems == {f"outbox/{CAP}.json": "capture_conflict"}
    assert (paths.receipts / f"{CAP}.json").read_bytes() == before
    source = store.get(receipt(paths, CAP).source_id)
    assert store.locate(source.source_id).payload_path.read_bytes() == TEXT


PUT_SCRIPT = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from weaver.archive import ArchiveStore, store as module
    from weaver.schema import Manifest

    archive, receipts, manifest, payload, mode = sys.argv[1:6]
    store = ArchiveStore(Path(archive), Path(receipts))
    if mode == "crash-after-index":
        original = store._index_capture
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            module.os._exit(9)  # power loss: no cleanup, no receipt
        store._index_capture = crash
    m = Manifest.model_validate_json(Path(manifest).read_text())
    print(store.put(m, Path(payload)).source_id)
    """
)


def run_put(tmp_path, m, mode="normal"):
    manifest_path = tmp_path / f"{m.capture_id}.manifest"
    manifest_path.write_text(m.model_dump_json())
    payload = tmp_path / "body.txt"
    if not payload.exists():  # never rewrite while another process reads it
        payload.write_bytes(TEXT)
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            PUT_SCRIPT,
            str(tmp_path / "archive"),
            str(tmp_path / "receipts"),
            str(manifest_path),
            str(payload),
            mode,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_process_killed_after_index_commit_recovers(tmp_path):
    (tmp_path / "archive").mkdir()
    (tmp_path / "receipts").mkdir()
    m = manifest()
    crashed = run_put(tmp_path, m, "crash-after-index")
    crashed.wait(timeout=30)
    assert crashed.returncode == 9
    assert not (tmp_path / "receipts" / f"{CAP}.json").exists()

    store = ArchiveStore(tmp_path / "archive", tmp_path / "receipts")
    assert store.status(CAP, m.sha256) == "stored"
    store.confirm(CAP)
    assert (tmp_path / "receipts" / f"{CAP}.json").exists()


def test_concurrent_processes_converge(tmp_path):
    (tmp_path / "archive").mkdir()
    (tmp_path / "receipts").mkdir()
    procs = [run_put(tmp_path, manifest(c)) for c in (CAP, CAP, CAP2, CAP2)]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        outputs.append(out.strip())
    assert len(set(outputs)) == 1
    store = ArchiveStore(tmp_path / "archive", tmp_path / "receipts")
    assert len(store.sources()) == 1
    assert len(store.captures_for(outputs[0])) == 2


def test_from_config_keeps_archive_outside_vault_and_sync(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'sync_root = "sync"\narchive_root = "archive"\nvault_root = "vault"\n'
        'state_root = "state"\nlog_root = "state/logs"\n'
    )
    config = load_config(config_file, environ={}, init_directories=True)
    store = ArchiveStore.from_config(config)
    m = manifest()
    (config.courier.outbox / m.payload).write_bytes(TEXT)
    source = store.put(m, config.courier.outbox / m.payload)

    located = store.locate(source.source_id).payload_path
    index = config.archive_root / "index.sqlite3"
    for private in (located, index):
        assert private.is_relative_to(config.archive_root)
        assert not private.is_relative_to(config.vault_root)
        assert not private.is_relative_to(config.sync_root)
    assert (config.courier.receipts / f"{CAP}.json").exists()
    assert list(config.vault_root.iterdir()) == []


INIT_SCRIPT = (
    "import sys\nfrom pathlib import Path\nfrom weaver.archive import ArchiveStore\n"
    "ArchiveStore(Path(sys.argv[1]), Path(sys.argv[2]))\n"
)


def test_many_processes_initialize_a_new_index(tmp_path):
    """PR #44 P1: journal_mode switching does not wait on busy locks."""
    for run in range(8):
        archive, receipts = tmp_path / f"a{run}", tmp_path / f"r{run}"
        archive.mkdir()
        receipts.mkdir()
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", INIT_SCRIPT, str(archive), str(receipts)],
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
        store = ArchiveStore(archive, receipts)
        assert store.sources() == []
