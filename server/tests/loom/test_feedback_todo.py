"""Approval-time feedback tasks use synthetic reviews and a fake model only."""

import json
from datetime import datetime, timedelta

import pytest
from server.tests.fake_responses import fixture_response
from server.tests.loom.test_reextract import draft, hashes, seed
from server.tests.loom.test_source_time import PROCESS_TIME, verdict
from server.tests.loom.test_summary_review import FakeSink, package

from weaver.context import ContextDictionary
from weaver.loom.reextract import reextract, render
from weaver.loom.summary_review import Prepared, SummaryReview
from weaver.loom.summary_state import SummaryState

SOURCE = datetime.fromisoformat("2026-11-12T11:06:00+09:00")
APPROVAL = datetime.fromisoformat("2026-11-12T13:19:00+09:00")
TODO = "확인 todo 오늘내 등록필요"


class FeedbackLlm:
    """Assert the prompt contract, then return its synthetic interpretation."""

    def __init__(self, *, mixed=False, day_offset=0, value=None):
        self.inputs = []
        self.mixed = mixed
        self.day_offset = day_offset
        self.value = value

    def generate_json(self, task, system, user, schema):
        if task == "loom.reextract_bootstrap":
            return schema.model_validate({"entries": []})
        assert task == "loom.approve"
        assert "요청 Thread 하나" in system
        assert "direction=받음(내가 할 일), from_feedback=true" in system
        assert "answers와 일반 의견" in system
        assert "approved_at과 timezone을 기준" in system
        assert "#158" not in system
        data = json.loads(user)
        self.inputs.append(data)
        rows = []
        if data["feedback"] == TODO:
            deadline = (
                (
                    datetime.fromisoformat(data["approved_at"])
                    + timedelta(days=self.day_offset)
                )
                .date()
                .isoformat()
            )
            rows.append(
                dict(
                    kind="요청",
                    content="위버 접수 건을 확인하고 등록한다.",
                    occurred_at=data["source_occurred_at"],
                    tags=["업무 운영"],
                    field="deadline",
                    value=self.value or deadline,
                    direction="보냄",
                    resolves=None,
                    retracts=None,
                    from_feedback=True,
                )
            )
        if self.mixed:
            rows.append(
                dict(
                    kind="요청",
                    content="출처에서 받은 서류 제출 요청",
                    occurred_at=data["source_occurred_at"],
                    tags=["업무 운영"],
                    field="deadline",
                    value=data["today"],
                    direction="받음",
                    resolves=None,
                    retracts=None,
                    from_feedback=False,
                )
            )
        return fixture_response(
            schema, dict(body="위버 접수 건", corrections=[], threads=rows)
        )


def approve(root, llm, feedback, *, approval=APPROVAL, fallback=False):
    SummaryState(root).save_package(
        package(),
        "합성 원문",
        source_occurred_at=SOURCE,
        source_time_is_fallback=fallback,
    )
    sink = FakeSink()
    review = SummaryReview(
        llm,
        ContextDictionary(root),
        sink,
        root / "archive",
        state_root=root,
    )
    decision = verdict().model_copy(update={"feedback": feedback})
    review.apply(package(), "합성 원문", decision, now=approval)
    review.apply(package(), "합성 원문", decision, now=PROCESS_TIME)
    assert len(llm.inputs) == 1
    return sink.drafts


@pytest.mark.parametrize(
    "action_feedback", [None, "좋은 요약입니다", "이름은 홍길동이 맞습니다"]
)
def test_answers_and_non_task_feedback_create_no_threads(tmp_path, action_feedback):
    assert approve(tmp_path, FeedbackLlm(), action_feedback) == []


@pytest.mark.parametrize("approval", [APPROVAL, APPROVAL + timedelta(days=5)])
def test_feedback_request_uses_approval_while_source_facts_keep_source_time(
    tmp_path, approval
):
    llm = FeedbackLlm(mixed=True)
    feedback, source = approve(tmp_path, llm, TODO, approval=approval)
    assert feedback.kind == "요청" and feedback.direction == "받음"
    assert feedback.value == approval.date().isoformat()
    assert feedback.occurred_at == approval and feedback.recorded_at == approval
    assert feedback.tags == ["업무 운영"]
    assert "from_feedback" not in feedback.model_dump()
    assert source.value == "2026-11-12" and source.occurred_at == SOURCE


def test_tomorrow_uses_approval_local_date_across_utc_boundary(tmp_path):
    approval = datetime.fromisoformat("2026-11-16T16:19:00+00:00")
    llm = FeedbackLlm(day_offset=1)
    (thread,) = approve(tmp_path, llm, TODO, approval=approval)
    assert llm.inputs[0]["approval_today"] == "2026-11-17"
    assert thread.value == "2026-11-18"


@pytest.mark.parametrize("fallback", [True, False])
@pytest.mark.parametrize("value", ["2026-11-11", "2026-11-12T12:00:00", "invalid"])
def test_feedback_deadline_validation_uses_approval_even_with_estimated_source(
    tmp_path, fallback, value
):
    (thread,) = approve(tmp_path, FeedbackLlm(value=value), TODO, fallback=fallback)
    assert thread.field is None and thread.value is None


@pytest.mark.parametrize("storage", ["applied_at", "summary", "thread", "fabric"])
def test_synthetic_reextract_preview_reproduces_original_approval(tmp_path, storage):
    old = draft("original", occurred_at=SOURCE, recorded_at=APPROVAL)
    seed(tmp_path, old=old)
    state = SummaryState(tmp_path)
    saved, script = state.load_package("pkg", 1)
    state.save_package(saved, script, source_occurred_at=SOURCE)
    with state.connect() as db:
        raw_verdict, raw_prepared = db.execute(
            "SELECT verdict, prepared FROM reviews"
        ).fetchone()
        decision = json.loads(raw_verdict)
        decision["feedback"] = TODO
        prepared = Prepared.model_validate_json(raw_prepared)
        if storage == "applied_at":
            prepared.applied_at = APPROVAL
        elif storage == "summary":
            from weaver.schema import EventSummary

            prepared.summary = EventSummary(
                schema_version=2,
                summary_id="a" * 64,
                source_id=saved.source_id,
                script=script,
                body=saved.draft_body,
                replaces=None,
                created_at=APPROVAL,
            )
        elif storage == "fabric":
            prepared.threads = []
        db.execute(
            "UPDATE reviews SET verdict=?, prepared=?",
            (json.dumps(decision), prepared.model_dump_json()),
        )
    before = hashes(tmp_path)
    llm = FeedbackLlm()
    report = reextract(tmp_path, tmp_path / "wiki", llm)
    assert not report.errors and not report.sources[0].error
    (thread,) = report.sources[0].new
    assert thread.kind == "요청" and thread.direction == "받음"
    assert thread.occurred_at == APPROVAL and thread.recorded_at > APPROVAL
    assert thread.tags == ["업무 운영"] and thread.value == "2026-11-12"
    assert llm.inputs[0]["approved_at"] == APPROVAL.isoformat()
    assert hashes(tmp_path) == before
    preview = render(report)
    assert 'deadline="2026-11-12"' in preview
    assert "occurred_at=2026-11-12T13:19:00+09:00" in preview
    print(preview)
