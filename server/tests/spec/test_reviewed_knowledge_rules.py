"""승인한 문서 한 장이 Thread로 바뀌는 규칙: 모델이 보낼 수 있는 모양과 지시문.

승인 응답은 Thread 종류와 field가 정한 모양만 통과한다. 제공자(Anthropic,
OpenAI)에게 보내는 JSON Schema와 서버 검증이 같은 모양을 받아들이고 같은
모양을 거절한다. 사전 항목은 정해진 종류와 길이 한도 안에서만 늘어난다.
모델에게 주는 지시문은 개조식 명사형, Thread마다 한 사실, 일정 제목 규칙을
담는다.
"""

import copy
from functools import cache

import anthropic
import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError
from server.tests.loom.test_summary_review import (
    NOW,
    FakeLlm,
    FakeSink,
    _approve_verdict,
    package,
)

from weaver.context import ContextDictionary
from weaver.llm.schemas import openai_schema
from weaver.loom.summary_review import ApprovedResponse, SummaryReview

PROVIDERS = {"anthropic": anthropic.transform_schema, "openai": openai_schema}
SCHEDULE_VALUE = {
    "start": "2026-01-01T09:00:00",
    "end": None,
    "timezone": None,
    "title": None,
    "description": None,
}


@cache
def provider_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(PROVIDERS[name](ApprovedResponse))


def providers_accepting(response: dict) -> list[str]:
    return [n for n in PROVIDERS if provider_validator(n).is_valid(response)]


def approval_response(**thread_changes: object) -> dict:
    thread = {
        "kind": "기록",
        "content": "자료 전달",
        "occurred_at": "2026-01-01T00:00:00Z",
        "tags": [],
        "counterpart": None,
        "field": None,
        "value": None,
        "direction": None,
        "resolves": None,
        "retracts": None,
        "blocked_by_index": None,
        "updates": None,
        "from_feedback": False,
    }
    thread.update(thread_changes)
    return {
        "new_entries": [],
        "body": "승인한 사실",
        "corrections": [],
        "threads": [thread],
    }


ACCEPTED_THREADS = {
    "request_with_direction": {"kind": "요청", "direction": "보냄"},
    "decision": {"kind": "결정"},
    "schedule": {"kind": "일정"},
    "record": {"kind": "기록"},
    "deadline": {"field": "deadline", "value": "tomorrow"},
    "status": {"field": "status", "value": "done"},
    "schedule_value": {"field": "schedule", "value": SCHEDULE_VALUE},
    "assignees": {
        "field": "assignees",
        "value": {"add": ["reviewer"], "remove": []},
    },
    "decision_resolving_a_request": {"kind": "결정", "resolves": "request"},
    "schedule_blocked_by_a_request": {"kind": "일정", "blocked_by_index": 0},
}


@pytest.mark.parametrize("changes", ACCEPTED_THREADS.values(), ids=ACCEPTED_THREADS)
def test_every_provider_schema_accepts_each_thread_shape(changes):
    response = approval_response(**changes)
    assert providers_accepting(response) == list(PROVIDERS)
    ApprovedResponse.model_validate(response)


REJECTED_THREADS = {
    "request_without_direction": {"kind": "요청", "direction": None},
    "direction_on_a_record": {"direction": "받음"},
    "schedule_value_of_wrong_shape": {
        "field": "schedule",
        "value": {"wrong": "shape"},
    },
    "assignees_value_of_wrong_shape": {
        "field": "assignees",
        "value": {"wrong": "shape"},
    },
    "status_value_of_wrong_shape": {"field": "status", "value": {"wrong": "shape"}},
    "value_without_field": {"value": "orphan value"},
    "missing_content": {"content": None},
    "tag_correction_kind": {"kind": "태그정정"},
    "correction_payload": {
        "correction": {"target": "old", "add": [], "remove": []},
    },
    "resolves_on_a_record": {"resolves": "request"},
    "block_index_on_a_record": {"blocked_by_index": 0},
}


@pytest.mark.parametrize("changes", REJECTED_THREADS.values(), ids=REJECTED_THREADS)
def test_every_provider_schema_and_the_server_reject_a_malformed_thread(changes):
    if changes.get("kind") == "요청":
        changes = {"direction": None, **changes}
    response = approval_response(**changes)
    assert providers_accepting(response) == []
    with pytest.raises(ValidationError):
        ApprovedResponse.model_validate(response)


def test_negative_block_index_is_rejected_by_the_server_even_if_the_provider_drops_minimums():
    # Anthropic strips numeric minimums from its schema, so only the server enforces it.
    response = approval_response(kind="일정", blocked_by_index=-1)
    with pytest.raises(ValidationError):
        ApprovedResponse.model_validate(response)


def test_new_dictionary_entries_are_limited_to_known_kinds():
    ok = approval_response()
    ok["new_entries"] = [{"name": "표준", "kind": "project", "aliases": ["별칭"]}]
    assert providers_accepting(ok) == list(PROVIDERS)
    ApprovedResponse.model_validate(ok)
    bad = copy.deepcopy(ok)
    bad["new_entries"][0]["kind"] = "arbitrary"
    assert providers_accepting(bad) == []
    with pytest.raises(ValidationError):
        ApprovedResponse.model_validate(bad)


@pytest.mark.parametrize(
    "entries",
    [
        [{"name": "name", "kind": "topic"}] * 51,
        [{"name": "x" * 121, "kind": "topic"}],
        [{"name": "name", "kind": "topic", "aliases": ["x" * 121]}],
        [{"name": "name", "kind": "topic", "aliases": ["alias"] * 21}],
    ],
    ids=["51_entries", "long_name", "long_alias", "21_aliases"],
)
def test_new_dictionary_entries_respect_count_and_length_limits(entries):
    response = approval_response()
    response["new_entries"] = entries
    with pytest.raises(ValidationError):
        ApprovedResponse.model_validate(response)


def test_approval_prompt_asks_for_noun_phrase_facts_and_schedule_titles(tmp_path):
    seen: list[str] = []

    class Llm(FakeLlm):
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            seen.append(system)
            return super().generate_json(task, system, user, schema)

    SummaryReview(
        Llm(),
        ContextDictionary(tmp_path / "state"),
        FakeSink(),
        tmp_path / "archive",
        state_root=tmp_path / "state",
    ).approve(package(), "원문", _approve_verdict(), now=NOW)
    (prompt,) = seen
    for rule in (
        "개조식 한 줄",
        "명사형",
        "Thread마다",
        "합치지",
        "title",
        "30자",
        "description",
    ):
        assert rule in prompt, rule
    assert "날짜·시각을 넣지 않는다" in prompt
    assert "알 수 있는 문장" not in prompt
