"""Projection retries use private markers and never hold the domain lock during I/O."""

import fcntl
import os

from weaver.loom.projection import ReviewProjection
from weaver.loom.summary_state import SummaryState
from weaver.schema import ReviewPackage


def package():
    return ReviewPackage.model_validate(
        {
            "schema_version": 2,
            "package_id": "pkg",
            "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "revision": 1,
            "draft_body": "synthetic",
            "questions": [],
            "stage": "반영",
            "stage_times": {},
        }
    )


def test_completed_marker_survives_failure_and_successful_retry_is_consumed(
    tmp_path, monkeypatch, caplog
):
    state = SummaryState(tmp_path / "state")
    item = package()
    with state.connect() as db:
        db.execute(
            "INSERT INTO pending_projections VALUES (?, ?, ?)",
            ("pkg", 1, item.model_dump_json()),
        )
    projection = ReviewProjection(tmp_path / "proposals")

    def fail(*args):
        raise OSError("synthetic shared-folder error")

    with monkeypatch.context() as patch:
        patch.setattr("weaver.loom.projection.write_json_atomic", fail)
        assert projection.recover(state) == []
    assert "review projection unavailable" in caplog.text
    with state.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pending_projections").fetchone()[0] == 1
    assert projection.recover(state) == ["pkg.r1.json"]
    with state.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pending_projections").fetchone()[0] == 0
    path = projection.directory / "pkg.r1.json"
    assert ReviewPackage.model_validate_json(path.read_bytes()) == item
    path.unlink()  # file Courier consumed the completed package
    assert projection.recover(state) == []
    assert not path.exists()


def test_recover_shared_io_runs_outside_lock_and_ignores_unrelated_prepared(
    tmp_path, monkeypatch
):
    import weaver.loom.projection as module

    state = SummaryState(tmp_path / "state")
    item = package().model_copy(update={"stage": "검토"})
    state.save_package(item, "synthetic")
    with state.connect() as db:
        # This is deliberately not JSON: an unrelated historical result is never decoded.
        db.execute(
            "INSERT INTO reviews VALUES ('other', 1, '{}', 'unrelated-invalid-json', 'APPLIED')"
        )
    write = module.write_json_atomic

    def unlocked(path, value):
        fd = os.open(state.path.with_suffix(".lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        write(path, value)

    monkeypatch.setattr(module, "write_json_atomic", unlocked)
    assert ReviewProjection(tmp_path / "proposals", strict=True).recover(state) == [
        "pkg.r1.json"
    ]
