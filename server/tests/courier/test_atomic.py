import json
import os

import pytest

from weaver.courier import write_bytes_atomic, write_json_atomic
from weaver.courier.atomic import temporary_name
from weaver.schema import Receipt

from .conftest import BODY, CAP, example, manifest_for


def receipt():
    return Receipt.model_validate(example("Receipt"))


def test_atomic_write_publishes_complete_json(tmp_path):
    final = tmp_path / f"{CAP}.json"
    write_json_atomic(final, receipt())
    assert json.loads(final.read_text()) == example("Receipt")
    assert [p.name for p in tmp_path.iterdir()] == [final.name]


@pytest.mark.parametrize("stage", ["write", "fsync", "replace"])
def test_failure_never_exposes_partial_json(tmp_path, monkeypatch, stage):
    final = tmp_path / f"{CAP}.json"
    previous = b'{"previous": true}'
    final.write_bytes(previous)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    if stage == "write":
        real_fdopen = os.fdopen

        def failing_fdopen(fd, mode):
            stream = real_fdopen(fd, mode)
            stream.write(b'{"partial":')  # some bytes reach the temp file
            stream.flush()
            stream.write = boom
            return stream

        monkeypatch.setattr(os, "fdopen", failing_fdopen)
    elif stage == "fsync":
        # The flush goes through full_fsync (F_FULLFSYNC on macOS, else fsync).
        from weaver.courier import atomic

        monkeypatch.setattr(atomic, "full_fsync", boom)
    else:
        monkeypatch.setattr(os, stage, boom)
    with pytest.raises(OSError):
        write_json_atomic(final, receipt())
    # Final name still holds the old, complete content; no temp leaks.
    assert final.read_bytes() == previous
    assert [p.name for p in tmp_path.iterdir()] == [final.name]


def test_new_file_absent_when_rename_fails(tmp_path, monkeypatch):
    final = tmp_path / f"{CAP}.json"
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        write_bytes_atomic(final, b"{}")
    assert list(tmp_path.iterdir()) == []


def test_interrupted_write_leaves_only_ignored_temp(courier, paths, captures):
    # Simulate a crash after the temp was written but before os.replace.
    (paths.outbox / f"{CAP}.txt").write_bytes(BODY)
    final = paths.outbox / f"{CAP}.json"
    temp = temporary_name(final)
    temp.write_text(json.dumps(manifest_for())[:40])
    assert temp.parent == final.parent and temp.name.startswith(".")
    assert not final.exists()
    report = courier.scan_once()
    assert report.accepted == [] and report.problems == {}


def test_full_fsync_uses_f_fullfsync_on_darwin(monkeypatch, tmp_path):
    import fcntl

    from weaver.courier import atomic

    calls = []
    monkeypatch.setattr(atomic.sys, "platform", "darwin")
    monkeypatch.setattr(atomic, "_F_FULLFSYNC", 51)
    monkeypatch.setattr(fcntl, "fcntl", lambda fd, op: calls.append(op))
    monkeypatch.setattr(atomic.os, "fsync", lambda fd: calls.append("fsync"))
    with (tmp_path / "f").open("wb") as stream:
        atomic.full_fsync(stream.fileno())
    assert calls == [51]


def test_full_fsync_falls_back_to_fsync(monkeypatch, tmp_path):
    import fcntl

    from weaver.courier import atomic

    calls = []

    def unsupported(fd, op):
        raise OSError(25, "not supported")

    monkeypatch.setattr(atomic.sys, "platform", "darwin")
    monkeypatch.setattr(atomic, "_F_FULLFSYNC", 51)
    monkeypatch.setattr(fcntl, "fcntl", unsupported)
    monkeypatch.setattr(atomic.os, "fsync", lambda fd: calls.append("fsync"))
    with (tmp_path / "f").open("wb") as stream:
        atomic.full_fsync(stream.fileno())
    assert calls == ["fsync"]


def test_atomic_write_uses_full_fsync(monkeypatch, tmp_path):
    from weaver.courier import atomic

    synced = []
    real = atomic.full_fsync
    monkeypatch.setattr(atomic, "full_fsync", lambda fd: (synced.append(fd), real(fd)))
    atomic.write_bytes_atomic(tmp_path / "x.json", b"{}")
    assert len(synced) == 2  # file, then directory
