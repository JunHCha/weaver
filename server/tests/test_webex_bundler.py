"""Stage 7: Space-day close, parts, corrections, exclusion and the Loom hand-off.

Synthetic Webex fixtures only (assumed REST shape, see test_webex_poller); no real
token, API, LLM or user vault. Time is KST (the default settings time zone).
"""

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from weaver.schema import Manifest, Receipt
from weaver.shepherd.outbox import BundleKey, CapacityError
from weaver.spinner.webex import WebexBundle
from weaver.webex.bundler import MAX_PAYLOAD_BYTES, close_ready, split_parts

from .test_webex_poller import page, setup  # noqa: F401

KST = ZoneInfo("Asia/Seoul")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=KST).astimezone(UTC)


def msg(mid: str, when: datetime, text: str = "synthetic", **extra):
    return {
        "id": mid,
        "roomId": "S",
        "created": when.isoformat(),
        "text": text,
        **extra,
    }


def states(box) -> dict[tuple[str, int, int], str]:
    return {(k.day, k.part, k.revision): state for k, state in box.bundle_keys()}


def key(box, day: str, part: int = 0, revision: int = 0) -> BundleKey:
    return BundleKey(box.instance_id, "S", day, part, revision)


def frozen(box, k: BundleKey) -> tuple[WebexBundle, Manifest]:
    entry = box.get(k.capture_id)
    return (
        WebexBundle.model_validate_json(entry.payload or b""),
        Manifest.model_validate_json(entry.manifest or b""),
    )


def feed(items: list):
    def respond(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}])
        return page(sorted(items, key=lambda m: m["created"], reverse=True))

    return respond


def test_day_closes_only_after_midnight_and_a_later_successful_poll(setup):  # noqa: F811
    make, box, clock, _ = setup
    items = [msg("m1", at(19, 10)), msg("m2", at(20, 20))]
    clock.now = at(20, 21)
    poller, _ = make(feed(items))
    poller.tick()
    assert states(box) == {
        ("2026-11-19", 0, 0): "pending",
        ("2026-11-20", 0, 0): "buffer",
    }
    clock.now = at(20, 23, 59)
    poller.tick()
    assert states(box)[("2026-11-20", 0, 0)] == "buffer"  # the day is not over
    # After midnight the day stays open until a poll succeeds.
    clock.now = at(21, 0, 10)
    broken, _ = make(lambda req: httpx.Response(503))
    broken.tick()
    assert states(box)[("2026-11-20", 0, 0)] == "buffer"
    clock.now = at(21, 0, 20)
    poller, _ = make(feed(items))
    poller.tick()
    assert states(box)[("2026-11-20", 0, 0)] == "pending"
    bundle, manifest = frozen(box, key(box, "2026-11-20"))
    assert (bundle.day, bundle.part, bundle.part_count, bundle.revision) == (
        "2026-11-20",
        0,
        1,
        0,
    )
    assert bundle.corrects_capture_id is None
    assert [m.message_id for m in bundle.messages] == ["m2"]
    assert (manifest.channel, manifest.kind, manifest.capture_host) == (
        "webex",
        "text",
        "mac",
    )
    assert manifest.occurred_at == datetime(2026, 11, 20, tzinfo=KST)
    assert manifest.payload == manifest.capture_id + ".txt"
    assert manifest.captured_at == at(21, 0, 20)  # frozen once, at close time


def test_closed_bytes_survive_polls_and_restart(setup):  # noqa: F811
    make, box, clock, _ = setup
    items = [msg("m1", at(19, 10))]
    clock.now = at(20, 21)
    poller, _ = make(feed(items))
    poller.tick()
    k = key(box, "2026-11-19")
    before = box.get(k.capture_id)
    clock.now = at(20, 22)
    poller, _ = make(feed(items))  # restart: same ID, bytes and Manifest
    poller.tick()
    assert box.get(k.capture_id) == before


def test_large_day_splits_deterministically_into_parts_one_source_each(setup):  # noqa: F811
    make, box, clock, _ = setup
    items = [msg(f"m{n}", at(19, 8, n), "가" * 120) for n in range(7)]
    # Keep the day open: only the first poll sees it, before midnight.
    clock.now = at(19, 23)
    poller, _ = make(feed(items))
    poller.max_payload_bytes = 1500
    poller.tick()
    assert states(box) == {("2026-11-19", 0, 0): "buffer"}
    original = box.get(key(box, "2026-11-19").capture_id).payload
    # Capacity failure keeps the exact unsplit buffer and does not close anything.
    box.free_bytes = lambda: 0
    clock.now = at(20, 0, 10)
    with pytest.raises(CapacityError):
        poller.tick()
    assert states(box) == {("2026-11-19", 0, 0): "buffer"}
    assert box.get(key(box, "2026-11-19").capture_id).payload == original
    box.free_bytes = lambda: 10**12
    poller.tick()
    parts = sorted(
        (k for k, _ in box.bundle_keys() if k.day == "2026-11-19"),
        key=lambda k: k.part,
    )
    assert len(parts) >= 3
    assert [k.part for k in parts] == list(range(len(parts)))
    seen: list[str] = []
    for k in parts:
        bundle, manifest = frozen(box, k)
        assert len(box.get(k.capture_id).payload or b"") <= 1500
        assert (bundle.part, bundle.part_count) == (k.part, len(parts))
        assert manifest.capture_id == k.capture_id
        seen += [m.message_id for m in bundle.messages]
    assert seen == [f"m{n}" for n in range(7)]
    assert len({k.capture_id for k in parts}) == len(parts)
    # The split is a pure function of the buffered messages.
    again = split_parts(
        WebexBundle.model_validate_json(original or b""), "2026-11-19", 1500
    )
    assert [[m.message_id for m in c] for c in again] == [
        [m.message_id for m in frozen(box, k)[0].messages] for k in parts
    ]


def test_64_mib_is_the_default_limit_and_a_message_over_it_is_held(setup):  # noqa: F811
    assert MAX_PAYLOAD_BYTES == 64 * 1024**2
    make, box, clock, _ = setup
    notices: list[str] = []
    box.notify = notices.append
    items = [msg("big", at(19, 9), "가" * 400), msg("ok", at(18, 9), "short")]
    clock.now = at(20, 0, 10)
    poller, _ = make(feed(items))
    poller.max_payload_bytes = 500
    poller.tick()
    # The oversized message holds its own day; the other day closes normally.
    assert states(box) == {
        ("2026-11-19", 0, 0): "buffer",
        ("2026-11-18", 0, 0): "pending",
    }
    holds = poller.state.spaces["S"].holds
    assert holds == {"2026-11-19#0": "message_too_large"}
    assert notices == ["webex_message_too_large"]


def test_excluded_space_is_never_closed_and_bytes_survive(setup):  # noqa: F811
    make, box, clock, settings = setup
    items = [msg("m1", at(19, 10))]
    clock.now = at(19, 21)
    poller, _ = make(feed(items))
    poller.tick()
    k = key(box, "2026-11-19")
    before = box.get(k.capture_id)
    assert before.state == "buffer"
    clock.now = at(20, 0, 10)
    excluded, _ = make(feed(items), replace(settings, exclude_space_ids=("S",)))
    excluded.tick()
    assert box.space_excluded("S")
    assert box.get(k.capture_id) == before
    # Even a caller that believes the Space is synced cannot close it.
    result = close_ready(
        box, timezone="Asia/Seoul", now=clock.now, eligible=lambda *_: True
    )
    assert result.closed == [] and result.held == {}
    assert box.get(k.capture_id) == before
    # Lifting the exclusion resumes collection and the same buffer then closes.
    clock.now = at(20, 1)
    resumed, _ = make(feed(items))
    resumed.tick()
    assert box.get(k.capture_id).state == "pending"


def test_late_update_and_late_message_become_linked_corrections(setup):  # noqa: F811
    make, box, clock, _ = setup
    items = [msg("m1", at(19, 10), "first"), msg("m2", at(20, 20))]
    clock.now = at(20, 21)
    poller, _ = make(feed(items))
    poller.tick()
    original = key(box, "2026-11-19")
    approved_bytes = box.get(original.capture_id)
    assert approved_bytes.state == "pending"
    # An edit arrives after close: found by the separate overlap scan.
    items[0] = msg("m1", at(19, 10), "edited", updated=at(20, 22).isoformat())
    clock.now = at(20, 22)
    poller, _ = make(feed(items))
    poller.tick()
    first = key(box, "2026-11-19", revision=1)
    assert box.get(first.capture_id).depends_on == original.capture_id
    bundle, _ = frozen(box, first)
    assert bundle.corrects_capture_id == original.capture_id
    assert [(m.message_id, m.text) for m in bundle.messages] == [("m1", "edited")]
    # A message that was created before midnight but arrived late.
    items.append(msg("m3", at(19, 23), "late"))
    clock.now = at(20, 23)
    poller, _ = make(feed(items))
    poller.tick()
    second = key(box, "2026-11-19", revision=2)
    assert box.get(second.capture_id).depends_on == first.capture_id
    assert [m.message_id for m in frozen(box, second)[0].messages] == ["m3"]
    # The earlier Source request is never rewritten.
    assert box.get(original.capture_id) == approved_bytes


def test_missing_message_is_only_observed_never_a_correction_or_rewrite(setup):  # noqa: F811
    make, box, clock, _ = setup
    items = [msg("m1", at(19, 10)), msg("m2", at(20, 20))]
    clock.now = at(20, 21)
    poller, _ = make(feed(items))
    poller.tick()
    snapshot = {k.capture_id: box.get(k.capture_id) for k, _ in box.bundle_keys()}
    del items[0]

    def respond(req):
        if req.url.path.startswith("/v1/messages/"):
            return httpx.Response(404)  # access revocation cannot be told apart
        return feed(items)(req)

    clock.now = at(20, 22)
    poller, _ = make(respond)
    poller.tick()
    space = poller.state.spaces["S"]
    assert space.observations["m1"].result == "unverified"
    assert {
        k.capture_id: box.get(k.capture_id) for k, _ in box.bundle_keys()
    } == snapshot


def test_correction_part_follows_predecessor_part_zero_when_split_differently(
    setup,  # noqa: F811
):
    from .shepherd.test_outbox import manifest

    _, box, _, _ = setup
    one = key(box, "2026-11-19", 0, 0)
    box.buffer(one, b"x")
    box.close(one, manifest(one, b"x"))
    assert box.correction_parent(key(box, "2026-11-19", 1, 1)) == one.capture_id
    assert box.correction_parent(key(box, "2026-11-19", 0, 1)) == one.capture_id
    assert box.correction_parent(one) is None


def test_correction_rule_is_one_replaceable_point():
    from types import SimpleNamespace

    from weaver.runtime.summary import SummaryProcessor
    from weaver.spinner.webex import WebexProvenance
    from weaver.webex.review_rule import (
        CorrectionCase,
        ReviewDecision,
        ReviewEveryCorrection,
    )

    value = WebexProvenance(
        source_id="src_X",
        sha256="0" * 64,
        instance_id="i",
        space_id="S",
        messages=[],
        day="2026-11-19",
        revision=2,
        corrects_capture_id="cap_prev",
    )
    provenance = SimpleNamespace(get=lambda _: value)
    source = SimpleNamespace(source_id="src_X")
    default = SummaryProcessor(None, None, None, webex=provenance)  # type: ignore[arg-type]
    assert isinstance(default.correction_rule, ReviewEveryCorrection)
    assert default._correction_decision(source).action == "review"  # type: ignore[arg-type]
    seen: list[CorrectionCase] = []

    class Supersede:
        def decide(self, case: CorrectionCase) -> ReviewDecision:
            seen.append(case)
            return ReviewDecision("supersede", ("src_OLD",))

    custom = SummaryProcessor(
        None,
        None,
        None,  # type: ignore[arg-type]
        webex=provenance,  # type: ignore[arg-type]
        correction_rule=Supersede(),
    )
    decision = custom._correction_decision(source)  # type: ignore[arg-type]
    assert (decision.action, decision.supersedes) == ("supersede", ("src_OLD",))
    assert seen[0].revision == 2 and seen[0].corrects_capture_id == "cap_prev"
    # Originals are never offered to the rule.
    value.revision = 0
    assert custom._correction_decision(source).action == "review"  # type: ignore[arg-type]
    assert len(seen) == 1


def test_policy_runs_before_the_rule_and_a_skip_is_never_final():
    from types import SimpleNamespace

    from weaver.runtime.summary import SummaryProcessor
    from weaver.spinner.webex import WebexProvenance
    from weaver.webex.review_rule import ReviewDecision

    value = WebexProvenance(
        source_id="src_X",
        sha256="0" * 64,
        instance_id="i",
        space_id="S",
        messages=[],
        day="2026-11-19",
        revision=1,
    )
    order: list[str] = []
    spinner = SimpleNamespace(spin=lambda source: order.append("spin"))
    policy = SimpleNamespace(check=lambda provenance: order.append("policy"))

    class Skip:
        def decide(self, case):
            order.append("rule")
            return ReviewDecision("skip")

    processor = SummaryProcessor(
        spinner,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        policy=policy,  # type: ignore[arg-type]
        webex=SimpleNamespace(get=lambda _: value),  # type: ignore[arg-type]
        correction_rule=Skip(),
    )
    source = SimpleNamespace(source_id="src_X", channel="webex")
    # "waiting_llm" is the loop's re-evaluable state; "done" would be permanent.
    assert processor.process(source, threading.Event()) == "waiting_llm"  # type: ignore[arg-type]
    assert order == ["spin", "policy", "rule"]
    assert processor.process(source, threading.Event()) == "waiting_llm"  # type: ignore[arg-type]
    assert order.count("rule") == 2  # asked again, not remembered as done


def test_integration_parts_corrections_receipt_loss_and_one_review_per_source(
    tmp_path,
):
    """Poller -> close -> loopback Courier -> archive -> Spinner/Loom, one page each."""
    from fastapi.testclient import TestClient

    from weaver.archive import ArchiveStore
    from weaver.context import ContextDictionary
    from weaver.llm.policy import LlmPolicy
    from weaver.loom.summary import SummaryDrafts
    from weaver.mobile.api import create_app
    from weaver.mobile.store import MobileStore
    from weaver.runtime.gate import LlmGate, LlmStatus
    from weaver.runtime.summary import SummaryLookup, SummaryProcessor
    from weaver.shepherd.outbox import Outbox, OutboxLimits
    from weaver.shepherd.registry import Registry
    from weaver.spinner import Spinner
    from weaver.spinner.webex import WebexProvenanceStore
    from weaver.webex.poller import Poller
    from weaver.webex.settings import WebexSettings

    from .mobile.helpers import write_config
    from .test_call_single_pass import Llm
    from .test_webex_poller import Clock, Tokens

    config = write_config(tmp_path / "cfg")
    store = MobileStore(config.state_root)
    token_file = tmp_path / "secrets" / "courier.token"
    Registry(store).register("mac-webex-primary", token_file)
    headers = {"Authorization": "Bearer " + token_file.read_text().strip()}
    server = TestClient(
        create_app(config, None, store=store, local_bind=("127.0.0.1", 8788)),
        base_url="http://127.0.0.1:8788",
        client=("127.0.0.1", 40000),
    )
    archive = ArchiveStore.from_config(config)
    dropped: list[str] = []
    puts: list[str] = []

    def bridge(request: httpx.Request) -> httpx.Response:
        result = server.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={**dict(request.headers), **headers},
        )
        if request.method == "PUT":
            puts.append(request.url.path)
            if request.url.path.endswith("/manifest") and not dropped:
                dropped.append(request.url.path)
                raise httpx.ReadTimeout("lost Receipt response")  # server committed
        return httpx.Response(result.status_code, content=result.content)

    courier = httpx.Client(
        base_url="http://127.0.0.1:8788/mobile/v1/",
        transport=httpx.MockTransport(bridge),
    )
    settings = WebexSettings(
        request_budget_total=20,
        request_budget_incremental=10,
        request_budget_overlap=5,
        request_budget_deletion=2,
        reconcile_interval_seconds=300,
    )
    box = Outbox(
        config.state_root / "shepherds" / settings.instance_id / "outbox.sqlite3",
        settings.instance_id,
        OutboxLimits(min_free_bytes=1),
    )
    clock = Clock()
    items = [msg(f"m{n}", at(19, 8, n), "가" * 120) for n in range(5)]
    clock.now = at(20, 21)

    def poller_for() -> Poller:
        poller = Poller(
            settings,
            box,
            Tokens(),
            httpx.Client(transport=httpx.MockTransport(feed(items))),
            clock=clock,
            jitter=lambda: 0.25,
        )
        poller.max_payload_bytes = 1100  # stand-in for the 64 MiB part boundary
        return poller

    poller_for().tick()
    original = sorted(
        (k for k, _ in box.bundle_keys() if k.revision == 0), key=lambda k: k.part
    )
    assert len(original) >= 2

    # The first manifest response is lost: Receipt GET recovers, no re-upload.
    box.deliver(courier)
    assert box.get(original[0].capture_id).state == "pending"
    puts_before = len(puts)
    box.deliver(courier)
    assert box.get(original[0].capture_id).state == "received"
    assert dropped[0] not in puts[puts_before:]
    assert all(e.state == "received" for e in box.entries())

    state_root = config.state_root / "loom"
    llm = Llm()
    provenance = WebexProvenanceStore(state_root)
    drafts = SummaryDrafts(
        llm,
        ContextDictionary(state_root),
        tmp_path / "proposals",
        state_root=state_root,
    )
    processor = SummaryProcessor(
        Spinner(archive, state_root, configuration_version="t"),
        drafts,
        LlmGate(lambda: LlmStatus(True, "ok")),
        policy=LlmPolicy(provenance, excluded_space_ids=frozenset()),
        webex=provenance,
    )

    def sources(wanted: set[str] | None = None):
        found = []
        for entry in box.entries():
            if wanted is None or entry.capture_id in wanted:
                receipt = Receipt.model_validate_json(entry.receipt or b"")
                found.append(archive.get(receipt.source_id))
        return found

    for source in sources():
        assert processor.process(source, threading.Event()) == "done"
    packages = sorted(drafts.proposals.glob("*.r1.json"))
    assert len(packages) == len(original)  # one page per part Source
    approved = packages[0].read_bytes()
    first_package = json.loads(approved)
    with drafts.state.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)",
            (first_package["package_id"], 1, "{}", "{}", "APPLIED"),
        )

    # A late edit after approval: its own linked Source and its own review page.
    items[0] = msg("m0", at(19, 8, 0), "edited later")
    clock.now = at(20, 22)
    poller_for().tick()
    corrections = [k for k, _ in box.bundle_keys() if k.revision == 1]
    assert corrections
    assert {box.get(k.capture_id).depends_on for k in corrections} <= {
        k.capture_id for k in original
    }
    box.deliver(courier)
    box.deliver(courier)
    new = sources({k.capture_id for k in corrections})
    assert len(new) == len(corrections)
    for source in new:
        assert processor.process(source, threading.Event()) == "done"
    lookup = SummaryLookup(tmp_path / "summaries", archive, provenance)
    # Wiki titles (and paths) carry only the date; the suffix is Loom-review only.
    for source in (*new, *sources()):
        assert lookup.title(source.source_id).endswith("Webex 대화")
    review_titles = [
        json.loads(p.read_bytes())["title"] for p in drafts.proposals.glob("*.r1.json")
    ]
    assert any("정정 1" in t for t in review_titles)
    assert any(" (1/" in t for t in review_titles)
    # One page per Source; the approved page and its review state are untouched.
    after = sorted(drafts.proposals.glob("*.r1.json"))
    assert len(after) == len(original) + len(corrections)
    assert packages[0].read_bytes() == approved
    with drafts.state.connect() as db:
        stage = db.execute(
            "SELECT stage FROM reviews WHERE id=? AND revision=1",
            (first_package["package_id"],),
        ).fetchone()
    assert stage == ("APPLIED",)
    # Processing the same Source again never makes a second page.
    assert processor.process(new[0], threading.Event()) == "done"
    assert len(list(drafts.proposals.glob("*.r1.json"))) == len(after)


def test_open_day_edits_merge_into_one_rev0_and_only_a_closed_day_gets_rev1(setup):  # noqa: F811
    make, box, clock, _ = setup
    items = [msg("m1", at(19, 10), "v1"), msg("m2", at(19, 12), "other")]
    clock.now = at(19, 20)
    poller, _ = make(feed(items))
    poller.tick()
    items[0] = msg("m1", at(19, 10), "v2", updated=at(19, 21).isoformat())
    items.append(msg("m3", at(19, 15), "late but same open day"))
    clock.now = at(19, 22)
    poller, _ = make(feed(items))
    poller.tick()
    items[0] = msg("m1", at(19, 10), "v3", updated=at(19, 23).isoformat())
    clock.now = at(19, 23, 30)
    poller, _ = make(feed(items))
    poller.tick()
    assert states(box) == {("2026-11-19", 0, 0): "buffer"}  # no new revision yet
    # The next day closes one page with the latest content.
    clock.now = at(20, 1)
    poller, _ = make(feed(items))
    poller.tick()
    assert states(box) == {("2026-11-19", 0, 0): "pending"}
    bundle, _ = frozen(box, key(box, "2026-11-19"))
    assert {m.message_id: m.text for m in bundle.messages} == {
        "m1": "v3",
        "m2": "other",
        "m3": "late but same open day",
    }
    assert bundle.revision == 0 and bundle.corrects_capture_id is None
    # Only an observation after close is a correction (revision 1).
    items[0] = msg("m1", at(19, 10), "v4", updated=at(20, 2).isoformat())
    clock.now = at(20, 2, 30)
    poller, _ = make(feed(items))
    poller.tick()
    assert states(box) == {
        ("2026-11-19", 0, 0): "pending",
        ("2026-11-19", 0, 1): "pending",
    }
    correction, _ = frozen(box, key(box, "2026-11-19", revision=1))
    assert [(m.message_id, m.text) for m in correction.messages] == [("m1", "v4")]
    assert correction.corrects_capture_id == key(box, "2026-11-19").capture_id


def _ny_settings(settings):
    return replace(settings, timezone="America/New_York")


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_dst_end_day_is_25_hours_and_exact_midnight_belongs_to_the_new_day(setup):  # noqa: F811
    make, box, clock, settings = setup
    ny = ZoneInfo("America/New_York")
    items = [
        msg("before", _utc(2026, 11, 1, 3, 59, 59), "Oct 31 23:59:59 EDT"),
        msg("midnight", _utc(2026, 11, 1, 4, 0, 0), "Nov 1 00:00:00 EDT, exactly"),
        msg("first-1am", _utc(2026, 11, 1, 5, 30), "01:30 EDT"),
        msg("second-1am", _utc(2026, 11, 1, 6, 30), "01:30 EST, repeated hour"),
        msg("last", _utc(2026, 11, 2, 4, 59, 59), "Nov 1 23:59:59 EST"),
        msg("next", _utc(2026, 11, 2, 5, 0, 0), "Nov 2 00:00:00 EST, exactly"),
    ]
    clock.now = _utc(2026, 11, 3, 12)
    poller, _ = make(feed(items), _ny_settings(settings))
    poller.tick()
    keys = {k.day: k for k, _ in box.bundle_keys()}
    assert sorted(keys) == ["2026-10-31", "2026-11-01", "2026-11-02"]
    ids = {
        day: [m.message_id for m in frozen(box, k)[0].messages]
        for day, k in keys.items()
    }
    assert ids["2026-10-31"] == ["before"]
    assert ids["2026-11-01"] == ["midnight", "first-1am", "second-1am", "last"]
    assert ids["2026-11-02"] == ["next"]
    manifest = frozen(box, keys["2026-11-01"])[1]
    assert manifest.occurred_at == datetime(2026, 11, 1, tzinfo=ny)
    assert manifest.occurred_at.astimezone(UTC) == _utc(2026, 11, 1, 4)


def test_dst_start_day_is_23_hours_and_day_closes_only_after_local_midnight(setup):  # noqa: F811
    make, box, clock, settings = setup
    ny = ZoneInfo("America/New_York")
    items = [
        msg("est", _utc(2026, 3, 8, 6, 59), "01:59 EST"),
        msg("edt", _utc(2026, 3, 8, 7, 0), "03:00 EDT (02:00 never exists)"),
        msg("last", _utc(2026, 3, 9, 3, 59, 59), "Mar 8 23:59:59 EDT"),
        msg("next", _utc(2026, 3, 9, 4, 0, 0), "Mar 9 00:00:00 EDT, exactly"),
    ]
    clock.now = _utc(2026, 3, 9, 3, 59, 59)  # one second before local midnight
    poller, _ = make(feed(items[:2]), _ny_settings(settings))
    poller.tick()
    assert states(box) == {("2026-03-08", 0, 0): "buffer"}
    clock.now = _utc(2026, 3, 9, 5, 0, 1)
    poller, _ = make(feed(items), _ny_settings(settings))
    poller.tick()
    got = states(box)
    assert got[("2026-03-08", 0, 0)] == "pending"
    assert got[("2026-03-09", 0, 0)] == "buffer"  # that day has not ended
    bundle, manifest = frozen(box, key(box, "2026-03-08"))
    assert [m.message_id for m in bundle.messages] == ["est", "edt", "last"]
    assert manifest.occurred_at == datetime(2026, 3, 8, tzinfo=ny)
    assert manifest.occurred_at.astimezone(UTC) == _utc(2026, 3, 8, 5)


def test_capacity_error_stops_only_its_own_space_and_notice_fires_once(setup):  # noqa: F811
    from weaver.shepherd.outbox import Outbox

    _, box, clock, _ = setup
    notices: list[str] = []
    box.notify = notices.append
    for space in ("A", "B"):
        k = BundleKey(box.instance_id, space, "2026-11-19", 0, 0)
        payload = WebexBundle(
            format="weaver.webex.bundle.v1",
            instance_id=box.instance_id,
            space_id=space,
            messages=[
                {
                    "message_id": f"m-{space}",
                    "created_at": at(19, 10),
                    "author_display": None,
                    "text": "x",
                }
            ],
        )
        box.buffer(k, payload.model_dump_json().encode())
    real = Outbox.freeze_parts

    def failing(self, original, expected, parts):
        if original.space == "A":
            self.notify("outbox_capacity")
            raise CapacityError("outbox_capacity")
        return real(self, original, expected, parts)

    Outbox.freeze_parts = failing  # type: ignore[method-assign]
    try:
        result = close_ready(
            box, timezone="Asia/Seoul", now=at(20, 1), eligible=lambda *_: True
        )
    finally:
        Outbox.freeze_parts = real  # type: ignore[method-assign]
    assert result.capacity
    assert len(result.closed) == 1  # Space B still closed
    got = {k.space: state for k, state in box.bundle_keys()}
    assert got == {"A": "buffer", "B": "pending"}


def test_hold_notice_fires_once_names_the_cause_and_status_explains_it(setup):  # noqa: F811
    from typer.testing import CliRunner

    from weaver.webex.cli import webex_cli

    make, box, clock, _ = setup
    notices: list[str] = []
    box.notify = notices.append
    items = [msg("big", at(19, 9), "가" * 400)]
    clock.now = at(20, 0, 10)
    for minute in range(3):  # three ticks, one notice
        clock.now = at(20, 0, 10 + 10 * minute)
        poller, _ = make(feed(items))
        poller.max_payload_bytes = 500
        poller.tick()
    assert poller.state.spaces["S"].holds == {"2026-11-19#0": "message_too_large"}
    assert notices == ["webex_message_too_large"]
    # The default log text no longer claims the Shepherd stopped.
    import logging

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("weaver.shepherd")
    logger.addHandler(handler)
    try:
        from weaver.shepherd.outbox import Outbox

        Outbox._log_notice("webex_message_too_large")
        Outbox._log_notice("outbox_capacity")
    finally:
        logger.removeHandler(handler)
    text = [r.getMessage() for r in records]
    assert "stopped" not in text[0] and "webex_message_too_large" in text[0]
    assert text[1] == "Shepherd stopped: outbox_capacity"
    del CliRunner, webex_cli


def test_missing_correction_parent_is_held_notified_once_and_shown(setup, monkeypatch):  # noqa: F811
    import json as jsonlib

    from typer.testing import CliRunner

    from weaver.webex import cli as webex_cli_module

    make, box, clock, settings = setup
    notices: list[str] = []
    box.notify = notices.append
    items = [msg("m1", at(19, 10), "first"), msg("m2", at(20, 20))]
    clock.now = at(20, 21)
    poller, _ = make(feed(items))
    poller.tick()
    original = key(box, "2026-11-19")
    box.discard(original.capture_id)  # operator disposed the revision it follows
    items[0] = msg("m1", at(19, 10), "edited", updated=at(20, 22).isoformat())
    for minute in (22, 23):
        clock.now = at(20, minute)
        poller, _ = make(feed(items))
        poller.tick()
    assert poller.state.spaces["S"].holds == {
        "2026-11-19#1": "missing_correction_parent"
    }
    assert notices == ["webex_missing_correction_parent"]
    from weaver import app
    from weaver.config import load_config
    from weaver.shepherd import cli as shepherd_cli

    from .test_config import write_config

    config = load_config(write_config(box.path.parent), environ={})
    monkeypatch.setattr(app, "_load", lambda _: config)
    monkeypatch.setattr(shepherd_cli, "_outbox", lambda *args: (config, box))
    result = CliRunner().invoke(webex_cli_module.webex_cli, ["poll-status"])
    status = jsonlib.loads(result.stdout)
    assert status["held_buffers"] == 1
    assert status["hold_reasons"] == {"missing_correction_parent": 1}
    assert "discard" in status["operator_actions"][0]


def test_space_not_ready_to_close_is_reported_with_its_reason(setup):  # noqa: F811
    make, box, clock, _ = setup
    items = [msg("m1", at(19, 10))]
    clock.now = at(19, 21)
    poller, _ = make(feed(items))
    poller.tick()
    clock.now = at(20, 0, 10)

    def gap(req):
        if req.url.path == "/v1/rooms":
            return page([{"id": "S"}])
        return httpx.Response(403)

    poller, _ = make(gap)
    poller.tick()
    assert states(box) == {("2026-11-19", 0, 0): "buffer"}
    waiting = poller.state.spaces["S"].close_waiting
    assert waiting == {"2026-11-19": "space_access_gap"}
