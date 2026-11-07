"""Regression tests for PR #42 review findings."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from weaver.courier import Courier, RevisionError, write_json_atomic
from weaver.courier.atomic import temporary_name
from weaver.schema import Receipt

from .conftest import (
    BODY,
    CAP,
    PRP,
    example,
    manifest_for,
    proposal_for,
    verdict_for,
    write_json,
)

SERVER = Path(__file__).resolve().parents[2]


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def publish_capture(outbox, capture_id=CAP, body=BODY):
    (outbox / f"{capture_id}.txt").write_bytes(body)
    write_json(outbox / f"{capture_id}.json", manifest_for(capture_id, body))


# ---- P2-1: completed captures are not re-hashed / re-reported ---------------


def test_received_capture_whose_body_was_removed_is_duplicate(courier, paths, captures):
    publish_capture(paths.outbox)
    assert courier.scan_once().accepted == [CAP]
    (paths.outbox / f"{CAP}.txt").unlink()  # phone cleaned body after receipt
    report = courier.scan_once()
    assert report.duplicates == [CAP] and report.problems == {}


def test_completed_capture_skips_payload_hashing(courier, paths, captures, monkeypatch):
    publish_capture(paths.outbox)
    courier.scan_once()
    import hashlib

    def no_hash(*args):
        raise AssertionError("payload re-hashed")

    monkeypatch.setattr(hashlib, "sha256", no_hash)
    assert courier.scan_once().duplicates == [CAP]


# ---- P2-2: port failures stay per-file ---------------------------------------


def boom(*args):
    raise RuntimeError("store down")


@pytest.mark.parametrize(
    ("port", "method", "folder"),
    [
        ("captures", "status", "outbox"),
        ("captures", "confirm", "outbox"),
        ("revisions", "latest_rev", "decisions"),
        ("decisions", "is_applied", "decisions"),
    ],
)
def test_query_port_failure_is_reported_per_file(
    courier, paths, captures, revisions, decisions, monkeypatch, port, method, folder
):
    publish_capture(paths.outbox)
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    write_json(paths.decisions / f"{PRP}.r1.json", verdict_for())
    if method == "confirm":  # confirm is only asked for stored captures
        captures.stored[CAP] = manifest_for()["sha256"]
    doubles = {"captures": captures, "revisions": revisions, "decisions": decisions}
    monkeypatch.setattr(doubles[port], method, boom)
    report = courier.scan_once()
    failed = f"{folder}/{CAP if folder == 'outbox' else PRP + '.r1'}.json"
    assert report.problems[failed] == "store_failed"
    # Other folders in the same pass still ran and the file is preserved.
    if folder == "outbox":
        assert report.applied == [(PRP, 1)]
        assert report.duplicates == []
        assert (paths.outbox / f"{CAP}.json").exists()
    else:
        assert report.accepted == [CAP]
        assert (paths.decisions / f"{PRP}.r1.json").exists()


def test_proposal_scan_port_failure_is_reported(courier, paths, revisions):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    revisions.latest_rev = boom
    report = courier.scan_once()
    assert report.problems == {f"proposals/{PRP}.r1.json": "store_failed"}


# ---- P3-1: resurrected proposal whose verdict was already applied -----------


def test_resurrected_applied_latest_proposal_is_pruned(
    courier, paths, revisions, decisions
):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    write_json(paths.decisions / f"{PRP}.r1.json", verdict_for())
    assert courier.scan_once().applied == [(PRP, 1)]
    payload = proposal_for(rev=1).model_dump(mode="json", by_alias=True)
    write_json(paths.proposals / f"{PRP}.r1.json", payload)  # resync revives it
    report = courier.scan_once()
    assert report.pruned == [f"{PRP}.r1.json"]
    assert names(paths.proposals) == []


# ---- P3-2: stale atomic-write temporaries are swept --------------------------


def old(path, seconds=3600):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


def test_stale_own_temporaries_are_removed(courier, paths, revisions):
    receipt = temporary_name(paths.receipts / f"{CAP}.json")
    proposal = temporary_name(paths.proposals / f"{PRP}.r1.json")
    alert = temporary_name(
        paths.proposals / f"alert_{example('Alert')['alert_id']}.json"
    )
    fresh = temporary_name(paths.proposals / f"{PRP}.r2.json")
    for path in (receipt, proposal, alert, fresh):
        path.write_text("{")
    for path in (receipt, proposal, alert):
        old(path)
    foreign = paths.proposals / ".stfolder-like.tmp"
    foreign.write_text("")
    old(foreign)
    syncthing = paths.proposals / f".syncthing.{PRP}.r1.json.tmp"
    syncthing.write_text("")
    old(syncthing)
    courier.scan_once()
    assert names(paths.receipts) == []
    assert names(paths.proposals) == sorted([fresh.name, foreign.name, syncthing.name])


def test_phone_folders_temporaries_are_not_touched(courier, paths):
    temp = temporary_name(paths.outbox / f"{CAP}.json")
    temp.write_text("{")
    old(temp)
    courier.scan_once()
    assert temp.exists()


# ---- P3-3: no symlink swap / FIFO stalls -------------------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_payload_does_not_block_scan(paths):
    os.mkfifo(paths.outbox / f"{CAP}.txt")
    write_json(paths.outbox / f"{CAP}.json", manifest_for())
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from weaver.config import CourierPaths\n"
        "from weaver.courier import Courier\n"
        "class S:\n"
        "    def status(self, *a): return 'new'\n"
        "    def confirm(self, c): pass\n"
        "    def accept(self, c): pass\n"
        "    def latest_rev(self, p): return None\n"
        "    def is_applied(self, *a): return False\n"
        "    def apply(self, v): pass\n"
        "root = Path(sys.argv[1])\n"
        "p = CourierPaths(*(root / n for n in "
        "('outbox','receipts','proposals','decisions')))\n"
        "s = S()\n"
        "print(Courier(p, s, s, s).scan_once().problems)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(paths.outbox.parent)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "not_regular_file" in result.stdout


def test_payload_symlink_swapped_after_check_is_not_followed(
    courier, paths, captures, tmp_path, monkeypatch
):
    """Swap the sibling target for an outside link between resolve and open."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(BODY)
    target = paths.outbox / "sibling.txt"
    target.write_bytes(b"x" * len(BODY))
    os.symlink(target.name, paths.outbox / f"{CAP}.txt")
    write_json(paths.outbox / f"{CAP}.json", manifest_for())

    real_resolve = Path.resolve

    def swapping_resolve(self, *args, **kwargs):
        result = real_resolve(self, *args, **kwargs)
        if self.name == f"{CAP}.txt":
            target.unlink()
            os.symlink(outside, target)
        return result

    monkeypatch.setattr(Path, "resolve", swapping_resolve)
    report = courier.scan_once()
    assert captures.accepted == []
    assert report.problems[f"outbox/{CAP}.json"] in {"symlink", "payload_outside"}


def test_manifest_swapped_to_symlink_is_not_followed(
    courier, paths, captures, tmp_path, monkeypatch
):
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    manifest = write_json(paths.outbox / f"{CAP}.json", manifest_for())
    outside = write_json(tmp_path / "m.json", manifest_for())
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):
        # The listing saw a regular file; it becomes a link right before open.
        if Path(path) == manifest and not manifest.is_symlink():
            manifest.unlink()
            os.symlink(outside, manifest)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    report = courier.scan_once()
    assert captures.accepted == []
    assert report.problems[f"outbox/{CAP}.json"] == "symlink"


# ---- P3-4: publish only the durably recorded latest rev ---------------------


@pytest.mark.parametrize(("latest", "rev"), [(None, 1), (1, 2), (2, 1)])
def test_publish_requires_recorded_latest_rev(courier, paths, revisions, latest, rev):
    if latest is not None:
        revisions.latest[PRP] = latest
    with pytest.raises(RevisionError):
        courier.publish_proposal(proposal_for(rev=rev))
    assert names(paths.proposals) == []


# ---- P3-5: folder/io failures and interrupted cleanup -----------------------


def test_missing_folder_is_reported_and_others_continue(courier, paths, captures):
    publish_capture(paths.outbox)
    shutil.rmtree(paths.decisions)
    report = courier.scan_once()
    assert report.problems == {"decisions/": "folder_unreadable"}
    assert report.accepted == [CAP]


def test_cleanup_io_error_is_reported_and_retried(
    courier, paths, revisions, decisions, monkeypatch
):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    decision = write_json(paths.decisions / f"{PRP}.r1.json", verdict_for())
    real_unlink = Path.unlink

    def failing_unlink(self, missing_ok=False):
        if self == decision:  # crash after proposal removal, before decision
            raise PermissionError("denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    report = courier.scan_once()
    assert report.problems == {f"decisions/{decision.name}": "io_error"}
    assert names(paths.proposals) == [] and decision.exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)

    restarted = Courier(paths, courier._captures, revisions, decisions)
    report = restarted.scan_once()
    assert report.cleaned == [decision.name] and report.problems == {}
    assert decisions.calls == [(PRP, 1, "approve")]  # applied exactly once
    assert names(paths.decisions) == []


def test_receipt_atomic_write_is_unchanged(paths):
    final = paths.receipts / f"{CAP}.json"
    write_json_atomic(final, Receipt.model_validate(example("Receipt")))
    assert final.exists()
