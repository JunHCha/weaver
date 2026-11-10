"""Examples of the schema v2 contracts, independent of real services."""

import pytest
from pydantic import ValidationError

from weaver.schema.v2 import Question, ThreadDraft


@pytest.fixture
def draft():
    return {
        "thread_id": "thread-1",
        "kind": "기록",
        "content": "메모",
        "occurred_at": "2026-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T00:00:00Z",
        "tags": [],
        "field": None,
        "value": None,
        "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "actor": "human",
        "direction": None,
        "resolves": None,
        "retracts": None,
        "correction": None,
    }


def test_direction_is_required_only_for_requests(draft):
    assert ThreadDraft.model_validate(draft | {"kind": "요청", "direction": "받음"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"kind": "요청"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"direction": "보냄"})


def test_correction_only_for_tag_correction(draft):
    correction = {"target": "thread-2", "remove": ["옛태그"], "add": ["새태그"]}
    assert ThreadDraft.model_validate(
        draft | {"kind": "태그정정", "correction": correction}
    )
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"kind": "태그정정"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"correction": correction})


def test_resolves_only_on_decision(draft):
    assert ThreadDraft.model_validate(draft | {"kind": "결정", "resolves": "thread-2"})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"resolves": "thread-2"})


def test_question_has_exactly_three_choices():
    data = {
        "question": "어느 쪽인가요?",
        "rationale": "모호함",
        "choices": ["가", "나", "다"],
    }
    assert Question.model_validate(data)
    for choices in ([], ["가", "나"], ["가", "나", "다", "라"]):
        with pytest.raises(ValidationError):
            Question.model_validate(data | {"choices": choices})


def test_rejection_has_no_content(draft):
    assert ThreadDraft.model_validate(draft | {"kind": "거절", "content": None})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"kind": "거절"})


def test_sequence_is_assigned_by_sink(draft):
    from weaver.schema.v2 import Thread

    assert Thread.model_validate(draft | {"seq": 1}).seq == 1
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"seq": 1})
    with pytest.raises(ValidationError):
        Thread.model_validate(draft | {"seq": 0})


def test_review_package_question_limit():
    from weaver.schema.v2 import ReviewPackage

    question = {
        "question": "어느 쪽인가요?",
        "rationale": "모호함",
        "choices": ["가", "나", "다"],
    }
    data = {
        "schema_version": 2,
        "package_id": "package-1",
        "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "revision": 1,
        "draft_body": "요약",
        "questions": [question] * 3,
        "stage": "검토",
        "stage_times": {"검토": "2026-01-01T00:00:00Z"},
    }
    assert ReviewPackage.model_validate(data)
    with pytest.raises(ValidationError):
        ReviewPackage.model_validate(data | {"questions": [question] * 4})
    with pytest.raises(ValidationError):
        ReviewPackage.model_validate(data | {"schema_version": 1})


def test_verdict_action_requires_appropriate_input():
    from weaver.schema.v2 import Verdict

    data = {
        "schema_version": 2,
        "package_id": "package-1",
        "revision": 1,
        "action": "approve",
        "answers": [],
        "edited_body": None,
        "feedback": None,
    }
    assert Verdict.model_validate(data)
    assert Verdict.model_validate(data | {"action": "edit", "edited_body": "수정"})
    assert Verdict.model_validate(
        data | {"action": "feedback", "feedback": "더 자세히"}
    )
    assert Verdict.model_validate(data | {"action": "reject"})
    for invalid in (
        {"action": "edit"},
        {"action": "feedback"},
        {"feedback": "더 자세히"},
        {"action": "reject", "edited_body": "수정"},
    ):
        with pytest.raises(ValidationError):
            Verdict.model_validate(data | invalid)


def test_event_summary_address_and_version():
    from weaver.schema.v2 import EventSummary

    data = {
        "schema_version": 2,
        "summary_id": "a" * 64,
        "source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "script": "script-1",
        "body": "요약",
        "replaces": None,
        "created_at": "2026-01-01T00:00:00Z",
    }
    assert EventSummary.model_validate(data)
    for invalid in (
        {"summary_id": "wrong"},
        {"replaces": "wrong"},
        {"schema_version": 1},
        {"schema_version": True},
    ):
        with pytest.raises(ValidationError):
            EventSummary.model_validate(data | invalid)


def test_schedule_value_and_block_reference(draft):
    schedule = {
        "start": "2026-01-01T09:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
    }
    assert ThreadDraft.model_validate(
        draft
        | {
            "kind": "일정",
            "field": "schedule",
            "value": schedule,
            "blocked_by": "request-1",
        }
    )
    for invalid in (
        "2026-01-01",
        {},
        schedule | {"timezone": "No/Such_Zone"},
        schedule | {"start": "2026-01-01T09:00:00"},
    ):
        with pytest.raises(ValidationError):
            ThreadDraft.model_validate(draft | {"field": "schedule", "value": invalid})
    with pytest.raises(ValidationError):
        ThreadDraft.model_validate(draft | {"blocked_by": "request-1"})


def test_approved_schedule_contract():
    from weaver.schema.v2 import ApprovedSchedule

    data = {
        "schema_version": 2,
        "package_id": "package-1",
        "thread_id": "thread-1",
        "title": "회의",
        "start": "2026-01-01T09:00:00+09:00",
        "end": None,
        "timezone": "Asia/Seoul",
        "blocked_by": None,
        "summary_id": "a" * 64,
    }
    assert ApprovedSchedule.model_validate(data)
    for invalid in (
        {"end": "2025-01-01T00:00:00Z"},
        {"title": " "},
        {"summary_id": "bad"},
        {"schema_version": 1},
    ):
        with pytest.raises(ValidationError):
            ApprovedSchedule.model_validate(data | invalid)
