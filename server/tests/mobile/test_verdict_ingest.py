"""Real review/Fabric/Action journals, synthetic LLM and private test directories."""

import json

import pytest

from weaver.action import ActionJob
from weaver.config import CourierSettings
from weaver.context import ContextDictionary
from weaver.courier import Courier
from weaver.fabric import ThreadQuery
from weaver.ingest import CourierFileError, VerdictIngest
from weaver.loom.summary_review import SummaryReview
from weaver.mobile.applier import MobileApplier
from weaver.runtime.summary import ReviewAdapter, SummaryLookup
from weaver.schema import Verdict

from .helpers import NOW, Harness, verdict
from .test_reviews import operation, submit


class ScheduleLlm:
    def __init__(self):
        self.calls = 0

    def generate_json(self, task, system, user, schema):
        assert task == "loom.approve"
        self.calls += 1
        return schema.model_validate(
            {
                "body": "합성 회의 일정",
                "new_entries": [{"name": "업무 운영", "kind": "area", "aliases": []}],
                "corrections": [],
                "threads": [
                    {
                        "kind": "일정",
                        "content": "합성 회의",
                        "occurred_at": NOW.isoformat(),
                        "tags": ["업무 운영"],
                        "field": "schedule",
                        "value": {
                            "start": "2026-12-01T10:00:00+09:00",
                            "end": None,
                            "timezone": "Asia/Seoul",
                        },
                        "direction": None,
                        "resolves": None,
                        "retracts": None,
                    }
                ],
            },
        )

    def generate_text(self, *args):
        raise AssertionError("unexpected text generation")


def wire_domain(h, llm):
    review = SummaryReview(
        llm,
        ContextDictionary(h.config.state_root),
        h.fabric,
        h.config.archive_root / "summaries",
        state_root=h.config.state_root,
        proposals=h.config.courier.proposals,
    )
    adapter = ReviewAdapter(
        review, SummaryLookup(h.config.archive_root / "summaries", h.archive)
    )
    ingest = VerdictIngest(adapter, adapter)
    courier = Courier(h.config.courier, h.archive, adapter, adapter, ingest=ingest)
    applier = MobileApplier(h.store, ingest, h.actions)
    return adapter, courier, applier


@pytest.mark.parametrize("crash_at", ["operation_update", "decision_commit"])
def test_crash_replay_keeps_one_real_thread_and_action(tmp_path, monkeypatch, crash_at):
    h = Harness(tmp_path, 'transport = "http"\n')
    headers = h.pair()
    h.add_package()
    queued = submit(h, headers)
    assert queued.status_code == 202
    llm = ScheduleLlm()
    adapter, _, applier = wire_domain(h, llm)
    job = ActionJob(h.fabric, h.outbox, h.actions)
    job.run()  # initialize the outbox before the approval

    def crash(*args, **kwargs):
        raise SystemExit("synthetic process crash")

    with monkeypatch.context() as patch:
        if crash_at == "operation_update":
            patch.setattr(h.store, "update_operation", crash)
        else:
            append = h.fabric.append

            def append_then_crash(threads):
                append(threads)
                raise SystemExit("synthetic crash after Fabric commit")

            patch.setattr(h.fabric, "append", append_then_crash)
        with pytest.raises(SystemExit):
            applier.run()

    assert adapter.is_applied("pkg_a", 1) == (crash_at == "operation_update")
    assert operation(h, headers, queued)["state"] == "queued"
    before = h.fabric.entries(ThreadQuery(include_executions=True))
    assert len(before) == 1
    job.run()
    (action,) = h.outbox.all()

    # A new service/adapter reads the durable decision journal after restart.
    adapter, courier, applier = wire_domain(h, llm)
    applier.recover()
    applier.run()
    job.run()
    assert operation(h, headers, queued)["state"] == "applied"
    assert adapter.is_applied("pkg_a", 1)
    assert h.fabric.entries(ThreadQuery(include_executions=True)) == before
    assert h.outbox.all() == [action]
    assert llm.calls == 1

    # A late file replay is cleaned through that same journal, without new facts.
    path = h.config.courier.decisions / "pkg_a.r1.json"
    path.write_text(json.dumps(verdict()))
    report = courier.scan_once()
    assert report.applied == [] and report.cleaned == [path.name]
    assert not path.exists()
    job.run()
    assert h.fabric.entries(ThreadQuery(include_executions=True)) == before
    assert h.outbox.all() == [action]
    assert llm.calls == 1


def test_real_file_approval_then_http_replay_uses_same_journal(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair()
    h.add_package()
    queued = submit(h, headers)
    llm = ScheduleLlm()
    _, courier, applier = wire_domain(h, llm)
    path = h.config.courier.decisions / "pkg_a.r1.json"
    path.write_text(json.dumps(verdict()))
    assert courier.scan_once().applied == [("pkg_a", 1)]
    before = h.fabric.entries(ThreadQuery(include_executions=True))
    applier.run()
    assert operation(h, headers, queued)["state"] == "applied"
    assert h.fabric.entries(ThreadQuery(include_executions=True)) == before
    assert len(before) == llm.calls == 1


class FailingStores:
    """Revision and decision ports whose apply always fails (nothing applied)."""

    def __init__(self):
        self.apply_calls = 0

    def latest_rev(self, package_id):
        return 1

    def is_applied(self, package_id, revision):
        return False

    def apply(self, verdict):
        self.apply_calls += 1
        raise RuntimeError("synthetic apply failure")


def _apply_code(ingest, key, verdict_model):
    with pytest.raises(CourierFileError) as caught:
        ingest.apply(key, verdict_model)
    return caught.value.code


def test_file_and_http_keys_keep_separate_retry_counters():
    """Characterization: one revision retried via a file key and an HTTP key."""
    stores = FailingStores()
    clock = [0.0]
    ingest = VerdictIngest(
        stores,
        stores,
        retry=CourierSettings(
            max_attempts=2, backoff_seconds=10, max_backoff_seconds=10
        ),
        clock=lambda: clock[0],
    )
    model = Verdict.model_validate(verdict())
    file_key, http_key = "pkg_a.r1.json", "external:op_1"

    assert _apply_code(ingest, file_key, model) == "apply_failed"
    assert _apply_code(ingest, file_key, model) == "apply_backoff"
    # The HTTP key is not in backoff: its counter starts at its own first attempt.
    assert _apply_code(ingest, http_key, model) == "apply_failed"
    assert _apply_code(ingest, http_key, model) == "apply_backoff"
    assert stores.apply_calls == 2

    clock[0] = 10.0
    assert _apply_code(ingest, file_key, model) == "apply_stopped"
    assert stores.apply_calls == 3
    # Stopping the file path does not consume the HTTP path's second attempt.
    assert _apply_code(ingest, http_key, model) == "apply_stopped"
    assert stores.apply_calls == 4
    assert _apply_code(ingest, file_key, model) == "apply_stopped"
    assert _apply_code(ingest, http_key, model) == "apply_stopped"
    assert stores.apply_calls == 4
