"""Review packages and verdicts on the Courier transport."""

import json
from pathlib import Path

import pytest

from weaver.courier import CourierFileError
from weaver.schema import Alert, ReviewPackage, Verdict

from .conftest import (
    PKG,
    PKG2,
    assert_schema_valid,
    example,
    package_for,
    place_package,
    verdict_for,
    write_json,
)

ROOT = Path(__file__).resolve().parents[3]


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def test_publication_and_verdict_apply(courier, paths, revisions, decisions):
    revisions.latest[PKG] = 1
    target = place_package(paths, package_for())
    assert ReviewPackage.model_validate_json(target.read_bytes()) == package_for()
    assert_schema_valid("ReviewPackage", json.loads(target.read_text()))
    decision = write_json(paths.decisions / target.name, verdict_for())
    report = courier.scan_once()
    assert report.applied == [(PKG, 1)]
    assert decisions.calls == [(PKG, 1, "approve")]
    assert not decision.exists() and not target.exists()


def test_old_revision_that_reappears_is_pruned(courier, paths, revisions):
    revisions.latest[PKG] = 2
    write_json(
        paths.proposals / f"{PKG}.r1.json",
        package_for(revision=1).model_dump(mode="json"),
    )
    report = courier.scan_once()
    assert report.pruned == [f"{PKG}.r1.json"]
    assert names(paths.proposals) == []


@pytest.mark.parametrize(
    ("name", "payload", "latest", "code"),
    [
        (f"{PKG}.r1.json", verdict_for(PKG2, 1), 1, "id_mismatch"),
        (f"{PKG}.r1.json", verdict_for(PKG, 2), 2, "rev_mismatch"),
        (f"{PKG}.r1.json", verdict_for(PKG, 1), None, "unknown_package"),
        (f"{PKG}.r1.json", verdict_for(PKG, 1), 2, "stale_rev"),
        (f"{PKG}.r2.json", verdict_for(PKG, 2), 1, "unknown_rev"),
        (f"{PKG}.r1.json", {"schema_version": 1}, 1, "unknown_version"),
        (f"{PKG}.r1.json", {"schema_version": 2}, 1, "invalid_schema"),
    ],
)
def test_invalid_decisions_are_reported_and_kept(
    courier, paths, revisions, decisions, name, payload, latest, code
):
    if latest is not None:
        revisions.latest[PKG] = latest
    path = write_json(paths.decisions / name, payload)
    report = courier.scan_once()
    assert report.problems == {f"decisions/{name}": code}
    assert path.exists() and decisions.calls == []


def test_apply_failure_is_retried_and_unconfirmed_apply_is_reported(
    courier, paths, revisions, decisions
):
    revisions.latest[PKG] = 1
    path = write_json(paths.decisions / f"{PKG}.r1.json", verdict_for())
    decisions.failures = 1
    assert courier.scan_once().problems == {f"decisions/{path.name}": "apply_failed"}
    decisions.confirm = False
    assert courier.scan_once().problems == {
        f"decisions/{path.name}": "apply_unconfirmed"
    }
    decisions.confirm = True
    assert courier.scan_once().applied == [(PKG, 1)]


def test_applied_decision_that_resyncs_is_cleaned_without_reapplying(
    courier, paths, revisions, decisions
):
    revisions.latest[PKG] = 1
    decisions.applied.add((PKG, 1))
    path = write_json(paths.decisions / f"{PKG}.r1.json", verdict_for())
    report = courier.scan_once()
    assert report.cleaned == [path.name]
    assert decisions.calls == [] and not path.exists()


def test_conflicting_resynced_verdict_is_reported(courier, paths, revisions, decisions):
    def refuse(verdict):
        raise CourierFileError("verdict_conflict")

    decisions.validate_verdict = refuse
    revisions.latest[PKG] = 1
    path = write_json(paths.decisions / f"{PKG}.r1.json", verdict_for())
    assert courier.scan_once().problems == {
        f"decisions/{path.name}": "verdict_conflict"
    }
    assert decisions.calls == [] and path.exists()


def test_every_verdict_action_round_trips_through_the_schema():
    for action in ("approve", "edit", "feedback", "reject"):
        payload = verdict_for(action=action)
        assert_schema_valid("Verdict", payload)
        Verdict.model_validate(payload)


def test_alert_file_is_published_atomically_and_passes_schema(courier, paths):
    alert = Alert.model_validate(example("Alert"))
    target = courier.publish_alert(alert)
    assert target.name == f"alert_{alert.alert_id}.json"
    assert_schema_valid("Alert", json.loads(target.read_text()))
    assert courier.scan_once().problems == {}
    assert target.exists()
