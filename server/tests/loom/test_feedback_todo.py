"""A to-do typed as review feedback becomes a request dated by the approval.

The request's deadline is validated against the approval time (in the local
zone) even when the source time is only an estimate, while facts from the
source keep the source time. Answers and ordinary comments create no Thread;
a second or invalid feedback request is downgraded instead of failing the
approval; a re-extraction preview reproduces the original approval time and
refuses to guess it when it was not saved. Synthetic reviews and a fake model
only."""

import json
from datetime import date, datetime, timedelta

import pytest
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

    def __init__(
        self,
        *,
        mixed=False,
        day_offset=0,
        value=None,
        emit_feedback=None,
        extra_feedback=False,
        feedback_kind="요청",
        feedback_area="업무 운영",
        source_area="업무 운영",
    ):
        self.emit_feedback = emit_feedback
        self.extra_feedback = extra_feedback
        self.feedback_kind = feedback_kind
        self.feedback_area = feedback_area
        self.source_area = source_area
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
        assert "today가 아닌 approval_today와 timezone을 기준" in system
        assert "검토 질문과 답은 Thread가 아니지만" in system
        assert "#158" not in system
        data = json.loads(user)
        self.inputs.append(data)
        rows = []
        emit = (
            data["feedback"] == TODO
            if self.emit_feedback is None
            else self.emit_feedback
        )
        if emit:
            deadline = (
                date.fromisoformat(data["approval_today"] or data["today"])
                + timedelta(days=self.day_offset)
            ).isoformat()
            rows.append(
                dict(
                    kind=self.feedback_kind,
                    content="위버 접수 건을 확인하고 등록한다.",
                    occurred_at=data["source_occurred_at"],
                    tags=[self.feedback_area],
                    field="deadline",
                    value=self.value or deadline,
                    direction="보냄" if self.feedback_kind == "요청" else None,
                    resolves=None,
                    retracts=None,
                    from_feedback=True,
                )
            )
        if self.extra_feedback and rows:
            rows.append(
                rows[0]
                | {"occurred_at": PROCESS_TIME.isoformat(), "content": "중복 지시"}
            )
        if self.mixed:
            rows.append(
                dict(
                    kind="요청",
                    content="출처에서 받은 서류 제출 요청",
                    occurred_at=data["source_occurred_at"],
                    tags=[self.source_area],
                    field="deadline",
                    value=data["today"],
                    direction="받음",
                    resolves=None,
                    retracts=None,
                    from_feedback=False,
                )
            )
        return schema.model_validate(
            dict(
                body="위버 접수 건",
                corrections=[],
                threads=rows,
                new_entries=[dict(name="업무 운영", kind="area", aliases=[])],
            )
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


@pytest.mark.parametrize("action_feedback", [None, "좋은 요약입니다"])
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


@pytest.mark.parametrize("value", ["2026-11-11", "2026-11-12T12:00:00", "invalid"])
def test_feedback_deadline_validation_uses_approval_even_with_estimated_source(
    tmp_path, value
):
    (thread,) = approve(tmp_path, FeedbackLlm(value=value), TODO, fallback=True)
    assert thread.field is None and thread.value is None


@pytest.mark.parametrize("storage", ["applied_at", "summary"])
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


def test_only_first_model_marked_feedback_request_keeps_approval_origin(
    tmp_path, caplog
):
    llm = FeedbackLlm(emit_feedback=True, extra_feedback=True)
    first, downgraded = approve(tmp_path, llm, TODO)
    assert first.occurred_at == APPROVAL and first.direction == "받음"
    # The second model-marked request gets the normal source occurrence guard,
    # rather than receiving the first request's approval timestamp.
    assert downgraded.occurred_at == SOURCE and downgraded.direction == "보냄"
    assert "downgraded extra feedback request" in caplog.text
    assert "corrected future occurrence time" in caplog.text
    assert "corrected feedback request direction" in caplog.text
    assert first.content not in caplog.text and downgraded.content not in caplog.text


@pytest.mark.parametrize("feedback, kind", [(None, "요청"), (TODO, "기록")])
def test_invalid_feedback_origin_is_downgraded_without_failing_approval(
    tmp_path, caplog, feedback, kind
):
    (thread,) = approve(
        tmp_path, FeedbackLlm(emit_feedback=True, feedback_kind=kind), feedback
    )
    assert thread.kind == kind and thread.occurred_at == SOURCE
    assert thread.direction == ("보냄" if kind == "요청" else None)
    assert "downgraded invalid feedback origin" in caplog.text
    assert "corrected feedback request direction" not in caplog.text
    assert thread.content not in caplog.text


@pytest.mark.parametrize("mixed", [False, True])
def test_feedback_area_follows_source_threads_with_real_dictionary_areas(
    tmp_path, caplog, mixed
):
    context = ContextDictionary(tmp_path)
    context.add_entry("개인 행정", "area")
    context.add_entry("고객 업무", "area")
    threads = approve(
        tmp_path,
        FeedbackLlm(mixed=mixed, feedback_area="개인 행정", source_area="고객 업무"),
        TODO,
    )
    assert threads[0].tags == ["고객 업무" if mixed else "개인 행정"]
    if mixed:
        assert threads[1].tags == ["고객 업무"]
        assert "corrected feedback request area" in caplog.text
    else:
        assert "corrected feedback request area" not in caplog.text
    assert "개인 행정" not in caplog.text and "고객 업무" not in caplog.text


@pytest.mark.parametrize(
    "feedback, emits",
    [(TODO, True), ("좋은 요약입니다", False)],
)
def test_reextract_without_saved_approval_time_only_fails_for_emitted_feedback_request(
    tmp_path, feedback, emits
):
    # Even old Threads with plausible recorded_at must not restore approval time:
    # their recorded_at could have been produced by an earlier re-extraction.
    seed(tmp_path, old=draft("earlier_reextract", recorded_at=APPROVAL))
    state = SummaryState(tmp_path)
    saved, script = state.load_package("pkg", 1)
    state.save_package(saved, script, source_occurred_at=SOURCE)
    with state.connect() as db:
        (raw,) = db.execute("SELECT verdict FROM reviews").fetchone()
        decision = json.loads(raw)
        decision["feedback"] = feedback
        db.execute("UPDATE reviews SET verdict=?", (json.dumps(decision),))
    llm = FeedbackLlm(emit_feedback=emits, mixed=True)
    before = hashes(tmp_path)
    report = reextract(tmp_path, tmp_path / "wiki", llm)
    assert not report.errors
    assert llm.inputs[0]["approved_at"] is None
    assert llm.inputs[0]["approval_today"] is None
    plan = report.sources[0]
    if emits:
        assert "원래 승인 시각을 복원할 수 없어" in plan.error
        assert "원래 승인 시각을 복원할 수 없어" in render(report)
        assert not plan.new and not plan.batch
    else:
        assert plan.error is None
        assert len(plan.new) == 1 and plan.new[0].occurred_at == SOURCE
    assert hashes(tmp_path) == before


def test_downgraded_feedback_deadline_uses_source_validation(tmp_path):
    first, extra = approve(
        tmp_path,
        FeedbackLlm(extra_feedback=True, value="2026-11-13"),
        TODO,
        approval=APPROVAL + timedelta(days=5),
    )
    assert first.field is None and first.value is None
    assert extra.field == "deadline" and extra.value == "2026-11-13"
    assert extra.occurred_at == SOURCE
