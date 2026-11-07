import json
import logging

import pytest

from weaver.courier import Courier, RevisionError
from weaver.schema import Alert, Proposal

from .conftest import (
    PRP,
    PRP2,
    assert_schema_valid,
    example,
    proposal_for,
    verdict_for,
    write_json,
)


def names(directory):
    return sorted(p.name for p in directory.iterdir())


def decide(paths, proposal_id=PRP, rev=1, kind="approve", **overrides):
    payload = verdict_for(proposal_id, rev, kind) | overrides
    return write_json(paths.decisions / f"{PRP}.r{rev}.json", payload)


@pytest.mark.parametrize("kind", ["approve", "edit", "revise", "reject"])
def test_verdict_fixtures_match_shared_schema(kind):
    assert_schema_valid("Verdict", verdict_for(kind=kind))


def test_publish_new_rev_replaces_old(courier, paths, revisions):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    revisions.latest[PRP] = 2
    final = courier.publish_proposal(proposal_for(rev=2))
    assert names(paths.proposals) == [f"{PRP}.r2.json"]
    published = json.loads(final.read_text())
    assert_schema_valid("Proposal", published)
    assert Proposal.model_validate(published).rev == 2


def test_publish_refuses_older_rev(courier, paths, revisions):
    revisions.latest[PRP] = 2
    with pytest.raises(RevisionError):
        courier.publish_proposal(proposal_for(rev=1))
    assert names(paths.proposals) == []


def test_publish_keeps_other_proposals(courier, paths, revisions):
    revisions.latest |= {PRP: 1, PRP2: 1}
    courier.publish_proposal(proposal_for(PRP2, 1))
    courier.publish_proposal(proposal_for(PRP, 1))
    alert = Alert.model_validate(example("Alert"))
    courier.publish_alert(alert)
    assert names(paths.proposals) == [
        f"alert_{alert.alert_id}.json",
        f"{PRP}.r1.json",
        f"{PRP2}.r1.json",
    ]
    assert courier.scan_once().problems == {}


def test_out_of_order_r1_after_r2_is_pruned(courier, paths, revisions):
    revisions.latest[PRP] = 2
    courier.publish_proposal(proposal_for(rev=2))
    # r1 shows up afterwards (late sync / resurrected delete).
    write_json(
        paths.proposals / f"{PRP}.r1.json",
        proposal_for(rev=1).model_dump(mode="json", by_alias=True),
    )
    report = courier.scan_once()
    assert report.pruned == [f"{PRP}.r1.json"]
    assert names(paths.proposals) == [f"{PRP}.r2.json"]


@pytest.mark.parametrize(
    ("filename", "body_id", "body_rev", "code"),
    [
        (f"{PRP}.r2.json", PRP, 1, "rev_mismatch"),
        (f"{PRP}.r1.json", PRP2, 1, "id_mismatch"),
        (f"{PRP}.r3.json", PRP, 3, "unknown_rev"),
    ],
)
def test_forged_proposal_files_are_kept_and_reported(
    courier, paths, revisions, filename, body_id, body_rev, code
):
    revisions.latest[PRP] = 2
    payload = proposal_for(body_id, body_rev).model_dump(mode="json", by_alias=True)
    write_json(paths.proposals / filename, payload)
    assert courier.scan_once().problems == {f"proposals/{filename}": code}
    assert filename in names(paths.proposals)


def test_approve_is_applied_then_files_cleaned(courier, paths, revisions, decisions):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    decide(paths)
    report = courier.scan_once()
    assert report.applied == [(PRP, 1)]
    assert decisions.calls == [(PRP, 1, "approve")]
    assert names(paths.proposals) == [] and names(paths.decisions) == []


def test_revise_publishes_next_rev_and_cleans_old(courier, paths, revisions, decisions):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    decide(paths, kind="revise")

    def loom_revises(verdict):
        revisions.latest[PRP] = verdict.rev + 1  # Loom persists r2 first
        courier.publish_proposal(proposal_for(rev=verdict.rev + 1))

    decisions.on_apply = loom_revises
    courier.scan_once()
    assert names(paths.proposals) == [f"{PRP}.r2.json"]
    assert names(paths.decisions) == []


def test_late_r1_verdict_is_not_applied(courier, paths, revisions, decisions, caplog):
    revisions.latest[PRP] = 2
    courier.publish_proposal(proposal_for(rev=2))
    decide(paths, rev=1)
    caplog.set_level(logging.WARNING, "weaver.courier")
    for _ in range(2):
        report = courier.scan_once()
        assert report.problems == {f"decisions/{PRP}.r1.json": "stale_rev"}
    assert decisions.calls == []
    # Preserved with a warning; the current proposal is untouched.
    assert names(paths.decisions) == [f"{PRP}.r1.json"]
    assert names(paths.proposals) == [f"{PRP}.r2.json"]
    assert [r.getMessage() for r in caplog.records] == [
        f"courier decisions/{PRP}.r1.json: stale_rev"
    ]


def test_r2_verdict_before_r1_verdict(courier, paths, revisions, decisions):
    revisions.latest[PRP] = 2
    courier.publish_proposal(proposal_for(rev=2))
    decide(paths, rev=2)
    assert courier.scan_once().applied == [(PRP, 2)]
    decide(paths, rev=1)  # older verdict syncs in afterwards
    assert courier.scan_once().problems == {f"decisions/{PRP}.r1.json": "stale_rev"}
    assert decisions.calls == [(PRP, 2, "approve")]


def test_apply_failure_is_retried_then_cleaned(courier, paths, revisions, decisions):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    decide(paths, kind="edit")
    decisions.failures = 1
    report = courier.scan_once()
    assert report.problems == {f"decisions/{PRP}.r1.json": "apply_failed"}
    assert names(paths.decisions) == [f"{PRP}.r1.json"]
    assert names(paths.proposals) == [f"{PRP}.r1.json"]
    report = courier.scan_once()
    assert report.applied == [(PRP, 1)]
    assert names(paths.decisions) == [] and names(paths.proposals) == []


def test_unconfirmed_apply_keeps_files(courier, paths, revisions, decisions):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    decide(paths)
    decisions.confirm = False
    report = courier.scan_once()
    assert report.problems == {f"decisions/{PRP}.r1.json": "apply_unconfirmed"}
    assert names(paths.decisions) == [f"{PRP}.r1.json"]
    assert names(paths.proposals) == [f"{PRP}.r1.json"]


def test_reappearing_applied_verdict_is_not_reapplied(
    paths, captures, revisions, decisions
):
    revisions.latest[PRP] = 1
    courier = Courier(paths, captures, revisions, decisions)
    courier.publish_proposal(proposal_for(rev=1))
    decide(paths, kind="reject")
    courier.scan_once()
    # Same file resurrected by resync, seen by a restarted server.
    decide(paths, kind="reject")
    restarted = Courier(paths, captures, revisions, decisions)
    report = restarted.scan_once()
    assert report.applied == [] and report.cleaned == [f"{PRP}.r1.json"]
    assert decisions.calls == [(PRP, 1, "reject")]
    assert names(paths.decisions) == []


@pytest.mark.parametrize(
    ("filename", "overrides", "code"),
    [
        (f"{PRP}.r2.json", {"rev": 1}, "rev_mismatch"),
        (f"{PRP}.r1.json", {"proposal_id": PRP2}, "id_mismatch"),
        (f"{PRP}.r3.json", {"rev": 3}, "unknown_rev"),
        (f"{PRP}.r1.json", {"schema_version": 2}, "unknown_version"),
        (f"{PRP}.r1.json", {"verdict": "decision"}, "invalid_schema"),
        (f"{PRP}.r1.json", {"reject_reason": "made_up"}, "invalid_schema"),
        (f"{PRP}.r1.json", {"rev": 1.0}, "invalid_schema"),
    ],
)
def test_forged_or_invalid_verdicts_are_refused(
    courier, paths, revisions, decisions, filename, overrides, code
):
    revisions.latest[PRP] = 1
    courier.publish_proposal(proposal_for(rev=1))
    payload = verdict_for(PRP, 1) | overrides
    write_json(paths.decisions / filename, payload)
    assert courier.scan_once().problems == {f"decisions/{filename}": code}
    assert decisions.calls == []
    assert filename in names(paths.decisions)
    assert names(paths.proposals) == [f"{PRP}.r1.json"]


def test_verdict_for_unknown_proposal_is_kept(courier, paths, decisions):
    decide(paths)
    assert courier.scan_once().problems == {
        f"decisions/{PRP}.r1.json": "unknown_proposal"
    }
    assert decisions.calls == []


def test_partial_verdict_json_is_ignored_until_complete(
    courier, paths, revisions, decisions
):
    revisions.latest[PRP] = 1
    final = paths.decisions / f"{PRP}.r1.json"
    final.write_text(json.dumps(verdict_for())[:30])
    assert courier.scan_once().problems == {f"decisions/{final.name}": "invalid_json"}
    write_json(final, verdict_for())
    assert courier.scan_once().applied == [(PRP, 1)]


@pytest.mark.parametrize(
    "name",
    [
        f".syncthing.{PRP}.r1.json.tmp",
        f"~syncthing~{PRP}.r1.json.tmp",
        f"{PRP}.r01.json",
        f"{PRP}.r0.json",
    ],
)
def test_temporary_or_noncanonical_decision_names_are_ignored(
    courier, paths, revisions, decisions, name
):
    revisions.latest[PRP] = 1
    write_json(paths.decisions / name, verdict_for())
    report = courier.scan_once()
    assert report.applied == [] and report.problems == {}
