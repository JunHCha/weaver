"""Synthetic I3 crash/migration fixtures; never touch a user's vault or models."""

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
from server.tests.mobile.helpers import write_config

from weaver.alert import AlertHistory, ScheduleAlertDetector
from weaver.alert.delivery import AlertDelivery
from weaver.config import MobileSettings
from weaver.runtime.summary import ObservationPublisher
from weaver.schema import Alert

from .test_detector import NOW, PKG, SEOUL, at, schedule


def alert(index=0):
    return Alert(
        schema_version=2,
        alert_id=f"alt_01ARZ3NDEKTSV4RRFFQ69G5FA{index}",
        package_id=PKG,
        thread_id="thr_2",
        message="합성 일정 알림",
        created_at=NOW,
    )


def test_body_and_history_commit_atomically(tmp_path):
    history = AlertHistory(tmp_path / "alerts.sqlite3")
    with history._connect() as db:
        db.execute(
            "CREATE TRIGGER fail_delivery BEFORE INSERT ON alert_delivery "
            "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        history.stage(alert(), "thr_1")
    assert not history.issued(alert().alert_id)
    assert history.body(alert().alert_id) is None


def test_crash_before_and_after_file_publication_preserves_first_body(tmp_path):
    config = write_config(tmp_path)
    delivery = AlertDelivery(config)

    def detector():
        return ScheduleAlertDetector(
            AlertHistory(config.state_root / "alerts.sqlite3"),
            timezone=SEOUL,
            clock=lambda: NOW,
        )

    first = detector()
    old, new = schedule(1, at(10)), schedule(2, at(10, 17))
    [body] = first.detect(PKG, [new], [old, new])
    # Crash after atomic history/body commit, before external publication.
    assert delivery.history.issued(body.alert_id)
    assert detector().pending() == [body]
    restarted = detector()
    for item in restarted.pending():
        path = delivery.publish_alert(item)
        # Crash after file publication, before the projected marker.
        assert path.exists()
        assert detector().pending() == [body]
        assert AlertDelivery(config).history.body(body.alert_id) == body
        restarted.published(item)
    path.unlink()  # file consumer dismisses; normal ticks must not recreate it
    assert detector().detect(PKG, [new], [old, new]) == []
    assert detector().pending() == []
    assert not path.exists()


def test_only_extant_legacy_bodies_are_imported(tmp_path):
    config = write_config(tmp_path)
    config.courier.proposals.mkdir(parents=True, exist_ok=True)
    history = AlertHistory(config.state_root / "alerts.sqlite3")
    gone, extant = alert(0), alert(1)
    history.record(gone, "thr_1")
    history.record(extant, "thr_1")
    path = config.courier.proposals / f"alert_{extant.alert_id}.json"
    path.write_text(extant.model_dump_json())
    delivery = AlertDelivery(config)
    assert delivery.history.bodies() == [extant]
    assert delivery.history.basis(extant.alert_id) == "thr_1"
    assert delivery.history.body(gone.alert_id) is None
    assert delivery.history.bodies(pending=True) == []
    assert AlertDelivery(config).history.bodies() == [extant]


def test_http_owns_delivery_and_rollback_republishes_even_acked_bodies(tmp_path):
    config = write_config(tmp_path, '[mobile.features]\nalert = "http"\n')
    files = replace(config, mobile=MobileSettings())
    delivery = AlertDelivery(files)
    unread, dismissed = alert(0), alert(1)
    for body in (unread, dismissed):
        delivery.history.stage(body, "thr_1")
        delivery.publish_alert(body)
    http = AlertDelivery(config)
    assert list(config.courier.proposals.glob("alert_*.json")) == []
    assert http.history.bodies() == [unread, dismissed]
    assert http.history.ack("phone", dismissed.alert_id, NOW)
    assert http.publish_alert(unread) is None
    rollback = AlertDelivery(files)
    assert rollback.republish() == 2
    actual = [
        Alert.model_validate_json(p.read_bytes())
        for p in sorted(config.courier.proposals.glob("alert_*.json"))
    ]
    assert actual == [unread, dismissed]  # phone ledger decides local dismiss


def test_conflicting_or_invalid_file_is_preserved_and_blocks_migration(tmp_path):
    config = write_config(tmp_path, '[mobile.features]\nalert = "http"\n')
    delivery = AlertDelivery(config)
    original = alert()
    delivery.history.stage(original, "thr_1")
    config.courier.proposals.mkdir(parents=True, exist_ok=True)
    path = config.courier.proposals / f"alert_{original.alert_id}.json"
    changed = original.model_copy(update={"message": "충돌 본문"})
    path.write_text(changed.model_dump_json())
    with pytest.raises(ValueError, match="alert_body_conflict"):
        AlertDelivery(config)
    assert delivery.history.body(original.alert_id) == original
    assert path.read_text() == changed.model_dump_json()
    path.write_text("broken json")
    with pytest.raises(ValueError):
        AlertDelivery(config)
    assert path.read_text() == "broken json"


def test_retention_requires_every_device_ack_and_preserves_history(tmp_path):
    history = AlertHistory(tmp_path / "alerts.sqlite3")
    body = alert()
    history.stage(body, "thr_1")
    history.projected(body.alert_id)
    later = NOW + timedelta(days=100)
    assert history.collect(1, ["one", "two"], later) == 0
    history.ack("one", body.alert_id, NOW)
    assert history.collect(1, ["one", "two"], later) == 0
    history.ack("two", body.alert_id, later)
    assert history.collect(1, ["one", "two"], later) == 0
    assert history.collect(30, ["one", "two"], later + timedelta(days=29)) == 0
    # Creation and the first receipt are old; the last active receipt starts TTL.
    assert history.collect(30, ["one", "two"], later + timedelta(days=30)) == 1
    assert history.body(body.alert_id) is None
    with history._connect() as db:
        assert db.execute("SELECT count(*) FROM alert_ack").fetchone()[0] == 2
    assert history.issued(body.alert_id)


def test_retention_without_recipients_or_policy_keeps_body(tmp_path):
    history = AlertHistory(tmp_path / "alerts.sqlite3")
    body = alert()
    history.stage(body, "thr_1")
    history.projected(body.alert_id)
    later = NOW + timedelta(days=100)
    history.ack("one", body.alert_id, NOW)
    history.ack("two", body.alert_id, later)
    assert history.collect(None, ["one", "two"], later + timedelta(days=2)) == 0
    assert history.collect(1, [], later + timedelta(days=2)) == 0
    assert history.collect(1, ["one", "two"], later + timedelta(days=2)) == 1
    assert history.body(body.alert_id) is None
    assert history.issued(body.alert_id)
    assert history.basis(body.alert_id) == "thr_1"
    assert history.ack("one", body.alert_id, later)  # retries after GC are safe


def test_pending_publication_replays_without_current_domain_candidates(tmp_path):
    config = write_config(tmp_path)
    delivery = AlertDelivery(config)
    body = alert()
    delivery.history.stage(body, "thr_1")
    detector = ScheduleAlertDetector(delivery.history, timezone=SEOUL)
    publisher = ObservationPublisher(None, None, (detector, delivery))
    publisher._alert([], [])
    path = config.courier.proposals / f"alert_{body.alert_id}.json"
    assert Alert.model_validate_json(path.read_bytes()) == body
    assert delivery.history.bodies(pending=True) == []
    path.unlink()
    publisher._alert([], [])
    assert not path.exists()
