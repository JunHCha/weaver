"""TextSourceProcessor, LLM gate/key resolution and review without an LLM.

No real LLM, launchd or vault: fakes and pytest temporary directories only.
"""

import contextlib
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from weaver.config import CourierPaths, LlmConfig
from weaver.courier import Courier
from weaver.fabric import FabricStore, ThreadQuery
from weaver.llm.keys import KeyUnavailable, resolve_api_key
from weaver.loom import (
    CourierPublisher,
    CourierReviewAdapter,
    ReviewStore,
    VerdictHandler,
)
from weaver.runtime import Interrupted, LlmGate, LlmStatus, RuntimeLedger
from weaver.runtime.gate import ExtractionInvalid, LlmUnavailable
from weaver.runtime.pipeline import GatedLlm, TextSourceProcessor, llm_status
from weaver.schema import Proposal, Verdict
from weaver.schema.internal import EntityIndex, Extraction

ROOT = Path(__file__).resolve().parents[2]


def example(name):
    return json.loads((ROOT / "schema" / "examples" / f"{name}.json").read_text())


PROPOSAL = Proposal.model_validate(example("Proposal"))


def extraction() -> Extraction:
    return Extraction.model_validate(
        PROPOSAL.model_dump(
            mode="json", exclude={"schema_version", "proposal_id", "rev", "source"}
        )
    )


class Killed(BaseException):
    """Stands in for SIGKILL: nothing after it runs."""


class FakeExtractor:
    versions = {"prompt": "loom.extract.v1", "model": "fake"}

    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result if result is not None else extraction()
        self.error = error

    def extract(self, thread, candidates, known):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class FakeReviews:
    def __init__(self):
        self.proposals = {}
        self.applied = set()

    @contextlib.contextmanager
    def lock(self):
        yield

    def source_proposal(self, source_id):
        return self.proposals.get(source_id)

    def latest_rev(self, pid):
        return 1

    def is_applied(self, pid, rev):
        return (pid, rev) in self.applied


class FakeCourier:
    def __init__(self):
        self.published = []

    def publish_proposal(self, proposal):
        self.published.append(proposal.proposal_id)


class FakeProposer:
    def __init__(self, reviews, fail=None):
        self.reviews = reviews
        self.courier = FakeCourier()
        self.fail = fail
        self.builds = []

    def build(self, source, extraction, versions=None):
        self.builds.append(versions)
        if self.fail is not None:
            raise self.fail
        self.reviews.proposals[source.source_id] = PROPOSAL
        return PROPOSAL


def make(tmp_path, *, extractor=None, proposer=None, reviews=None, available=True):
    reviews = reviews or FakeReviews()
    spun = []
    processor = TextSourceProcessor(
        spinner=SimpleNamespace(spin=lambda s: spun.append(s) or "thread"),
        refiner=SimpleNamespace(select=lambda thread: ["candidate"]),
        extractor=extractor or FakeExtractor(),
        proposer=proposer or FakeProposer(reviews),
        reviews=reviews,
        entities=SimpleNamespace(index=lambda: EntityIndex(entities=[])),
        cache=RuntimeLedger(tmp_path / "runtime.sqlite3"),
        gate=LlmGate(lambda: LlmStatus(available, "configured")),
    )
    return processor, spun


SOURCE = SimpleNamespace(source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA0", channel="kakao")


def test_existing_r1_is_only_republished(tmp_path):
    reviews = FakeReviews()
    reviews.proposals[SOURCE.source_id] = PROPOSAL
    extractor = FakeExtractor()
    proposer = FakeProposer(reviews)
    processor, spun = make(
        tmp_path, extractor=extractor, proposer=proposer, reviews=reviews
    )
    assert processor.process(SOURCE, threading.Event()) == "done"
    assert proposer.courier.published == [PROPOSAL.proposal_id]
    assert spun == [] and extractor.calls == 0 and proposer.builds == []
    reviews.applied.add((PROPOSAL.proposal_id, 1))
    processor.process(SOURCE, threading.Event())
    assert proposer.courier.published == [PROPOSAL.proposal_id]  # applied: not again


def test_build_oserror_then_retry_reuses_extraction(tmp_path):
    extractor = FakeExtractor()
    reviews = FakeReviews()
    failing = FakeProposer(reviews, fail=OSError("publish"))
    processor, _ = make(
        tmp_path, extractor=extractor, proposer=failing, reviews=reviews
    )
    with pytest.raises(OSError):
        processor.process(SOURCE, threading.Event())
    healthy = FakeProposer(reviews)
    again, _ = make(tmp_path, extractor=extractor, proposer=healthy, reviews=reviews)
    assert again.process(SOURCE, threading.Event()) == "done"
    assert extractor.calls == 1
    assert healthy.builds == [FakeExtractor.versions]


def test_sigkill_after_extraction_restart_calls_extract_once(tmp_path):
    extractor = FakeExtractor()
    reviews = FakeReviews()
    processor, _ = make(
        tmp_path,
        extractor=extractor,
        proposer=FakeProposer(reviews, fail=Killed()),
        reviews=reviews,
    )
    with pytest.raises(Killed):
        processor.process(SOURCE, threading.Event())
    # "Restart": a new processor over the same state_root ledger.
    restarted_extractor = FakeExtractor()
    restarted, _ = make(tmp_path, extractor=restarted_extractor, reviews=reviews)
    assert restarted.process(SOURCE, threading.Event()) == "done"
    assert extractor.calls + restarted_extractor.calls == 1


def test_no_extraction_is_cached_too(tmp_path):
    extractor = FakeExtractor()
    extractor.result = None
    extractor.extract = lambda *a: (
        setattr(extractor, "calls", extractor.calls + 1),
        None,
    )[1]
    processor, _ = make(tmp_path, extractor=extractor)
    assert processor.process(SOURCE, threading.Event()) == "done"
    assert processor.process(SOURCE, threading.Event()) == "done"
    assert extractor.calls == 1


def test_invalid_extraction_output_is_a_contract_violation(tmp_path):
    processor, _ = make(tmp_path, extractor=FakeExtractor(error=ValueError("x")))
    with pytest.raises(ExtractionInvalid):
        processor.process(SOURCE, threading.Event())


def test_stop_before_llm_stage_interrupts(tmp_path):
    extractor = FakeExtractor()
    processor, spun = make(tmp_path, extractor=extractor)
    stop = threading.Event()
    stop.set()
    with pytest.raises(Interrupted):
        processor.process(SOURCE, stop)
    assert spun == [] and extractor.calls == 0


def test_llm_off_waits_after_local_stages(tmp_path):
    extractor = FakeExtractor()
    processor, spun = make(tmp_path, extractor=extractor, available=False)
    assert processor.process(SOURCE, threading.Event()) == "waiting_llm"
    assert len(spun) == 1 and extractor.calls == 0
    call = SimpleNamespace(source_id=SOURCE.source_id, channel="call")
    assert processor.process(call, threading.Event()) == "waiting_llm"
    assert len(spun) == 1  # call scripts are not even spun without the Reviser


# ---- key resolution / llm_status ---------------------------------------------


def key_file(tmp_path, content="  sk-test-key \n", mode=0o600):
    path = tmp_path / "anthropic.key"
    path.write_text(content)
    os.chmod(path, mode)
    return path


def test_llm_status_from_environment():
    config = LlmConfig("anthropic", "WEAVER_TEST_KEY")
    assert llm_status(config, {"WEAVER_TEST_KEY": "k"}) == LlmStatus(True, "configured")
    assert llm_status(config, {}).reason == "api_key_missing"
    assert llm_status(LlmConfig("anthropic"), {}).reason == "api_key_env_unset"
    assert llm_status(LlmConfig("none"), {}).reason == "backend_none"
    assert llm_status(LlmConfig("agent_sdk"), {}).reason == "backend_unsupported"


def test_key_file_wins_over_env_and_strips_whitespace(tmp_path):
    config = LlmConfig("anthropic", "WEAVER_TEST_KEY", key_file(tmp_path))
    assert resolve_api_key(config, {"WEAVER_TEST_KEY": "env"}) == "sk-test-key"
    # launchd passes no shell environment: the file alone is enough.
    assert llm_status(config, {}) == LlmStatus(True, "configured")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_key_file_with_group_or_other_bits_is_insecure(tmp_path, mode):
    config = LlmConfig("anthropic", None, key_file(tmp_path, mode=mode))
    assert llm_status(config, {}).reason == "api_key_file_insecure"


def test_key_file_symlink_missing_and_empty(tmp_path):
    real = key_file(tmp_path)
    link = tmp_path / "link.key"
    link.symlink_to(real)
    assert llm_status(LlmConfig("anthropic", None, link), {}).reason == (
        "api_key_file_insecure"
    )
    missing = LlmConfig("anthropic", None, tmp_path / "absent.key")
    assert llm_status(missing, {}).reason == "api_key_file_missing"
    empty = LlmConfig("anthropic", None, key_file(tmp_path, content=" \n"))
    with pytest.raises(KeyUnavailable) as error:
        resolve_api_key(empty, {})
    assert error.value.reason == "api_key_missing"
    assert str(tmp_path) not in str(error.value)


def test_config_accepts_absolute_key_file_only(tmp_path):
    from weaver.config import ConfigError, load_config

    roots = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
    for key in roots:
        (tmp_path / key).mkdir()
    for channel in ("outbox", "receipts", "proposals", "decisions"):
        (tmp_path / "sync_root" / channel).mkdir()
    base = "\n".join(f'{k} = "{k}"' for k in roots) + "\n[llm]\n"
    path = tmp_path / "config.toml"
    path.write_text(base + f'api_key_file = "{tmp_path / "anthropic.key"}"\n')
    assert load_config(path, environ={}).llm.api_key_file == tmp_path / "anthropic.key"
    path.write_text(base + 'api_key_file = "relative.key"\n')
    with pytest.raises(ConfigError, match="absolute"):
        load_config(path, environ={})
    path.write_text(base + f'api_key_file = "{tmp_path / "sync_root" / "k"}"\n')
    with pytest.raises(ConfigError, match="sync_root"):
        load_config(path, environ={})


# ---- review without an LLM ------------------------------------------------------


@pytest.fixture
def review(tmp_path):
    paths = CourierPaths(
        **{
            n: tmp_path / "sync" / n
            for n in ("outbox", "receipts", "proposals", "decisions")
        }
    )
    for path in (paths.outbox, paths.receipts, paths.proposals, paths.decisions):
        path.mkdir(parents=True)
    store = ReviewStore(tmp_path / "state" / "loom.sqlite3")
    proposal = Proposal.model_validate(example("Proposal"))
    proposal.entities[0].ask = None
    store.save_proposal(proposal)
    fabric = FabricStore(tmp_path / "state" / "fabric.sqlite3")
    sink = SimpleNamespace(record=lambda event: None)
    off = LlmGate(lambda: LlmStatus(False, "api_key_missing"))
    publisher = CourierPublisher(store)
    handler = VerdictHandler(store, fabric, publisher, sink, GatedLlm(None, off))
    adapter = CourierReviewAdapter(handler)
    publisher.courier = Courier(paths, None, adapter, adapter)
    publisher.publish_proposal(proposal)
    return SimpleNamespace(**locals())


def verdict(review, kind):
    data = example("Verdict") | {
        "proposal_id": review.proposal.proposal_id,
        "verdict": kind,
    }
    if kind == "revise":
        data["instruction"] = "다시 확인"
    result = Verdict.model_validate(data)
    (review.paths.decisions / f"{result.proposal_id}.r{result.rev}.json").write_text(
        result.model_dump_json()
    )
    return result


def test_without_llm_approve_applies_and_revise_is_refused(review):
    with pytest.raises(LlmUnavailable):
        review.handler.apply(verdict(review, "revise"))
    assert review.store.latest_rev(review.proposal.proposal_id) == 1
    assert list(review.paths.decisions.iterdir())  # kept for a later retry
    for leftover in review.paths.decisions.iterdir():
        leftover.unlink()
    entries = review.handler.apply(verdict(review, "approve"))
    assert len(entries) == 2
    assert len(review.fabric.entries(ThreadQuery())) == 2
