"""Option A: only the newest undecided correction of a Space-day stays reviewable.

Synthetic provenance/packages only; no LLM, Webex or user vault.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from weaver.loom.projection import ReviewProjection
from weaver.loom.summary_state import SummaryState
from weaver.mobile.feed import FeedProducer
from weaver.mobile.reviews import ReviewService
from weaver.mobile.store import MobileStore
from weaver.runtime.summary import SummaryCorrectionLedger, SummaryProcessor
from weaver.schema import ReviewPackage
from weaver.spinner.webex import WebexMessageRef, WebexProvenance, WebexProvenanceStore
from weaver.webex.review_rule import (
    CorrectionCase,
    ReviewEveryCorrection,
    SupersedeUnreviewedCorrections,
)

from .mobile.helpers import verdict

NOW = datetime(2026, 11, 20, tzinfo=UTC).isoformat()
DAY = "2026-11-19"


def sid(revision: int, part: int = 0) -> str:
    return f"src_01ARZ3NDEKTSV4RRFFQ69G5F{revision}{part}"


def provenance(revision: int, messages: list[str], *, part: int = 0, space="S"):
    return WebexProvenance(
        source_id=sid(revision, part),
        sha256="0" * 64,
        instance_id="i",
        space_id=space,
        day=DAY,
        part=part,
        revision=revision,
        messages=[
            WebexMessageRef(message_id=m, segment_id=f"s{n}")
            for n, m in enumerate(messages, 1)
        ],
    )


def package(source_id: str) -> ReviewPackage:
    return ReviewPackage.model_validate(
        {
            "schema_version": 2,
            "package_id": "pkg_" + source_id,
            "source_id": source_id,
            "revision": 1,
            "title": "합성",
            "source_text": "본문",
            "source_type": "webex",
            "draft_body": "초안",
            "questions": [],
            "stage": "검토",
            "stage_times": {"검토": NOW},
        }
    )


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = SummaryState(root / "state")
        self.webex = WebexProvenanceStore(root / "state")
        self.store = MobileStore(root / "mobile")
        self.reviews = ReviewService(self.state, self.store)
        self.feed = FeedProducer(self.store, self.reviews, None, lambda: 0)

    def add(self, value: WebexProvenance, *, packaged: bool = True) -> None:
        self.webex.save(value)
        if packaged:
            self.state.save_package(package(value.source_id), "script")

    def decide(self, source: WebexProvenance):
        rule = SupersedeUnreviewedCorrections(
            SummaryCorrectionLedger(self.webex, self.state)
        )
        return rule.decide(
            CorrectionCase(
                source.source_id,
                source.space_id,
                source.day,
                source.part,
                source.revision,
                None,
            )
        )

    def waiting(self) -> set[str]:
        return {p.source_id for p in self.reviews.open_packages()}

    def verdict_received(self, source_id: str) -> None:
        with self.state.connect() as db:
            db.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?)",
                ("pkg_" + source_id, 1, "{}", "{}", "PREPARED"),
            )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def run(world: World, value: WebexProvenance):
    """Process one correction Source through the real processor with fake Loom."""
    created: list[str] = []

    class Drafts:
        state = world.state
        projection = ReviewProjection(world.root / "proposals", enabled=False)

        def create(self, source_id, *args, **kwargs):
            created.append(source_id)
            package_ = package(source_id)
            world.state.save_package(package_, "script")
            return package_, "script"

    processor = SummaryProcessor(
        SimpleNamespace(spin=lambda source: SimpleNamespace(text="t")),  # type: ignore[arg-type]
        Drafts(),  # type: ignore[arg-type]
        SimpleNamespace(available=True),  # type: ignore[arg-type]
        policy=SimpleNamespace(check=lambda _: None),  # type: ignore[arg-type]
        webex=world.webex,
    )
    source = SimpleNamespace(
        source_id=value.source_id,
        channel="webex",
        hints=SimpleNamespace(counterpart=None),
        occurred_at=None,
        captured_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    outcome = processor.process(source, threading.Event())  # type: ignore[arg-type]
    return outcome, created


def test_default_rule_with_a_journal_is_option_a(world: World) -> None:
    processor = SummaryProcessor(
        None,  # type: ignore[arg-type]
        SimpleNamespace(state=world.state),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        webex=world.webex,
    )
    assert isinstance(processor.correction_rule, SupersedeUnreviewedCorrections)
    assert isinstance(
        SummaryProcessor(None, None, None).correction_rule,  # type: ignore[arg-type]
        ReviewEveryCorrection,
    )


def test_two_unreviewed_corrections_leave_only_the_newest_waiting(world: World) -> None:
    original = provenance(0, ["m1", "m2"])
    older = provenance(1, ["m1"])
    newer = provenance(2, ["m1", "m3"])
    world.add(original)
    world.add(older)
    assert world.waiting() == {sid(0), sid(1)}
    world.feed.reconcile()  # the phone already saw both pages
    # The newer correction is processed: its page appears, the older one is retired.
    world.webex.save(newer)
    outcome, created = run(world, newer)
    assert outcome == "done" and created == [sid(2)]
    assert world.waiting() == {sid(0), sid(2)}  # the original is never replaced
    assert world.state.superseded_sources() == {sid(1): sid(2)}
    # Nothing is deleted: package, provenance and Source records remain.
    assert world.state.load_package("pkg_" + sid(1), 1)[0].source_id == sid(1)
    assert world.webex.get(sid(1)) is not None
    # The phone feed turns the retired review into a tombstone.
    world.feed.reconcile()
    assert {
        k[1]: v.op for k, v in world.store.feed_current().items() if k[0] == "review"
    } == {
        "pkg_" + sid(0): "upsert",
        "pkg_" + sid(2): "upsert",
        "pkg_" + sid(1): "tombstone",
    }


def test_older_correction_processed_after_the_newer_one_is_never_reviewed(
    world: World,
) -> None:
    world.add(provenance(2, ["m1"]))
    older = provenance(1, ["m1"])
    world.webex.save(older)
    outcome, created = run(world, older)
    assert (outcome, created) == ("done", [])  # final, no page was created
    assert world.state.superseded_sources() == {sid(1): sid(2)}
    assert run(world, older) == ("done", [])  # idempotent on a retry


def test_a_correction_with_a_received_verdict_is_kept(world: World) -> None:
    older = provenance(1, ["m1"])
    world.add(older)
    world.verdict_received(sid(1))  # approval/review in flight
    newer = provenance(2, ["m1"])
    world.webex.save(newer)
    outcome, created = run(world, newer)
    assert (outcome, created) == ("done", [sid(2)])
    assert world.state.superseded_sources() == {}
    assert sid(1) in world.waiting()  # not PRUNED: it is still undecided
    assert not world.state.supersede(sid(1), sid(2))  # and refuses directly
    assert world.decide(older).action == "review"


def test_original_revision_zero_is_never_superseded(world: World) -> None:
    original = provenance(0, ["m1"])
    world.add(original)
    world.add(provenance(1, ["m1"]))
    assert world.decide(original).action == "review"
    assert not [s for s in world.state.superseded_sources() if s == sid(0)]


def test_superseded_state_survives_a_restart_and_blocks_a_verdict(
    world: World, tmp_path: Path
) -> None:
    from weaver.mobile.errors import ApiError

    world.add(provenance(1, ["m1"]))
    world.add(provenance(2, ["m1"]))
    assert world.state.supersede(sid(1), sid(2))
    reopened = World(tmp_path)  # new process, same files
    assert reopened.state.superseded_sources() == {sid(1): sid(2)}
    assert reopened.waiting() == {sid(2)}
    with pytest.raises(ApiError) as caught:
        reopened.reviews.submit(
            "dev",
            "pkg_" + sid(1),
            1,
            None,
            json.dumps(verdict("pkg_" + sid(1), 1)).encode(),
        )
    assert (caught.value.status, caught.value.args[0]) == (409, "review_superseded")


def test_a_correction_carrying_other_messages_is_not_dropped(world: World) -> None:
    older = provenance(1, ["m3"])  # a late message that the newer one lacks
    world.add(older)
    newer = provenance(2, ["m1"])
    world.webex.save(newer)
    outcome, created = run(world, newer)
    assert (outcome, created) == ("done", [sid(2)])
    assert world.state.superseded_sources() == {}
    assert world.waiting() == {sid(1), sid(2)}


def test_other_spaces_and_days_and_parts_are_independent(world: World) -> None:
    world.add(provenance(1, ["m1"], space="OTHER"))
    first = provenance(2, ["m1"], part=0)
    second = provenance(2, ["m2"], part=1)  # two parts of the newest revision
    world.add(first)
    world.add(second)
    assert world.decide(first).action == "review"
    assert world.decide(second).action == "review"
    assert world.state.superseded_sources() == {}


def test_file_projection_stops_offering_a_superseded_review(
    world: World, tmp_path: Path
) -> None:
    folder = tmp_path / "proposals"
    projection = ReviewProjection(folder)
    world.add(provenance(1, ["m1"]))
    world.add(provenance(2, ["m1"]))
    projection.recover(world.state)
    assert {p.name for p in folder.glob("*.json")} == {
        f"pkg_{sid(1)}.r1.json",
        f"pkg_{sid(2)}.r1.json",
    }
    assert world.state.supersede(sid(1), sid(2))
    projection.recover(world.state)
    assert {p.name for p in folder.glob("*.json")} == {f"pkg_{sid(2)}.r1.json"}


def test_superseded_review_follows_the_feed_transport_rules(
    world: World,
) -> None:
    from weaver.mobile.feed import FeedProducer, FeedSources

    http = [True]
    feed = FeedProducer(
        world.store,
        world.reviews,
        None,
        lambda: 0,
        sources=FeedSources(review_http=lambda: http[0]),
    )
    world.add(provenance(1, ["m1"]))
    world.add(provenance(2, ["m1"]))
    feed.reconcile()
    reviews = lambda: {  # noqa: E731
        k[1]: v.op for k, v in world.store.feed_current().items() if k[0] == "review"
    }
    assert reviews() == {"pkg_" + sid(1): "upsert", "pkg_" + sid(2): "upsert"}
    http[0] = False  # review back on files: the kind is dropped, nothing tombstoned
    assert world.state.supersede(sid(1), sid(2))
    feed.reconcile()
    assert reviews()["pkg_" + sid(1)] == "upsert"
    http[0] = True  # returns to http: the retired review is a tombstone
    feed.reconcile()
    assert reviews() == {"pkg_" + sid(1): "tombstone", "pkg_" + sid(2): "upsert"}
