"""#9 alert files: publisher wiring, Courier atomic writes and runtime assembly."""

import json
import logging
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from weaver.alert import AlertHistory, ScheduleAlertDetector
from weaver.config import CourierPaths, load_config
from weaver.courier import Courier, atomic
from weaver.fabric import FabricStore
from weaver.runtime.assemble import build_runtime
from weaver.runtime.pipeline import AlertingPublisher, NoAlerts

from .test_detector import NOW, SECRET, SEOUL, fact, moved

ROOT = Path(__file__).resolve().parents[3]


class Inner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def publish_proposal(self, proposal):
        self.calls.append(("proposal", proposal.rev))
        return "published"

    def clean_review(self, proposal_id, rev):
        self.calls.append(("clean", rev))


@pytest.fixture
def paths(tmp_path):
    folders = {}
    for name in ("outbox", "receipts", "proposals", "decisions"):
        folders[name] = tmp_path / "sync" / name
        folders[name].mkdir(parents=True)
    return CourierPaths(**folders)


@pytest.fixture
def setup(tmp_path, paths):
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append([fact(1, "date", "2026-11-10T15:00:00+09:00")])
    courier = Courier(paths, None, None, None)  # publish_alert uses paths only
    detector = ScheduleAlertDetector(
        fabric,
        AlertHistory(tmp_path / "alerts.sqlite3"),
        timezone=SEOUL,
        clock=lambda: NOW,
    )
    inner = Inner()
    return AlertingPublisher(inner, detector, courier), inner, paths


def alert_files(paths: CourierPaths) -> list[Path]:
    return sorted(paths.proposals.glob("alert_*.json"))


def test_alert_file_follows_proposal_and_passes_schema(setup):
    publisher, inner, paths = setup
    assert publisher.publish_proposal(moved()) == "published"
    [path] = alert_files(paths)
    payload = json.loads(path.read_text())
    schema = json.loads((ROOT / "schema" / "Alert.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    assert path.name == f"alert_{payload['alert_id']}.json"
    assert payload["proposal_id"] == moved().proposal_id
    assert payload["schedule_ref"] == "s1"
    assert "승인 전" in payload["message"]
    assert SECRET not in path.read_text()
    assert inner.calls == [("proposal", 1)]
    publisher.clean_review(moved().proposal_id, 1)
    assert inner.calls[-1] == ("clean", 1)


def test_retry_and_phone_deletion_do_not_recreate(setup):
    publisher, _, paths = setup
    publisher.publish_proposal(moved())
    [path] = alert_files(paths)
    path.unlink()  # the phone dismissed and removed it
    publisher.publish_proposal(moved())  # retry / restart republish
    publisher.publish_proposal(moved(rev=2))  # revise with the same change
    assert alert_files(paths) == []
    publisher.publish_proposal(moved("2026-11-13T15:00:00+09:00", rev=2))
    assert len(alert_files(paths)) == 1


def test_partial_write_never_exposes_final_file(setup, monkeypatch):
    publisher, _, paths = setup

    def broken(fd):
        raise OSError("disk full")

    monkeypatch.setattr(atomic, "full_fsync", broken)
    publisher.publish_proposal(moved())  # alert failure never blocks a proposal
    assert list(paths.proposals.iterdir()) == []
    monkeypatch.undo()
    publisher.publish_proposal(moved())  # not recorded, so the retry delivers it
    assert len(alert_files(paths)) == 1


def test_detector_failure_is_logged_without_content(tmp_path, paths, caplog):
    class Broken(NoAlerts):
        def detect(self, proposal):
            raise ValueError(SECRET)

    inner = Inner()
    publisher = AlertingPublisher(inner, Broken(), Courier(paths, None, None, None))
    with caplog.at_level(logging.WARNING):
        assert publisher.publish_proposal(moved()) == "published"
    assert "ValueError" in caplog.text and SECRET not in caplog.text


def test_proposal_failure_skips_alerts(setup):
    publisher, inner, paths = setup

    def fail(proposal):
        raise RuntimeError("publication failed")

    inner.publish_proposal = fail
    with pytest.raises(RuntimeError):
        publisher.publish_proposal(moved())
    assert alert_files(paths) == []


def test_runtime_wires_schedule_detector(tmp_path):
    roots = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
    path = tmp_path / "config.toml"
    path.write_text("".join(f'{k} = "{k}"\n' for k in roots))
    for key in roots:
        (tmp_path / key).mkdir()
    for channel in ("outbox", "receipts", "proposals", "decisions"):
        (tmp_path / "sync_root" / channel).mkdir()
    config = load_config(path, environ={})
    runtime = build_runtime(config, environ={})
    publisher = runtime.processor.proposer.courier
    assert isinstance(publisher, AlertingPublisher)
    assert isinstance(publisher.detector, ScheduleAlertDetector)
    assert (config.state_root / "alerts.sqlite3").exists()
