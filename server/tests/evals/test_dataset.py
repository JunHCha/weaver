"""Eval dataset format (#10): public samples, integrity and rejection rules."""

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from weaver.evals import (
    CASES_DIR,
    DatasetError,
    load_case,
    load_dataset,
    summarize,
    validate_case,
)
from weaver.schema.models import TIMESTAMP_PATTERN

ENT = re.compile(r"^ent_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
TEXT = "민서현 님이 다음 주 화요일 오후 3시에 예산 회의를 하자고 했다."


def excerpt(text: str = TEXT) -> dict[str, Any]:
    return {"confidence": 0.9, "ask": None, "thread_excerpt": text}


def extraction() -> dict[str, Any]:
    return {
        "summary": "예산 회의 일정",
        "thread_excerpt": TEXT,
        "entities": [
            {
                **excerpt(),
                "ref": "e1",
                "entity_id": "@seohyun",
                "type": "person",
                "name": "민서현",
                "aliases": [],
            },
            {
                **excerpt(),
                "ref": "e2",
                "entity_id": None,
                "type": "meeting",
                "name": "예산 회의",
                "aliases": [],
            },
        ],
        "schedules": [
            {
                **excerpt(),
                "ref": "s1",
                "title": "예산 회의",
                "start": "2031-03-11T15:00:00+09:00",
                "end": None,
                "entities": ["e1", "e2"],
                "entry_ref": "d1",
                "blocked_by_request": None,
            }
        ],
        "relations": [
            {
                **excerpt(),
                "from": "e1",
                "type": "참석",
                "to": "e2",
                "reason": None,
            }
        ],
        "entry_drafts": [
            {
                **excerpt(),
                "ref": "d1",
                "entities": ["e1", "e2"],
                "kind": "일정",
                "field": None,
                "value": "2031-03-11T15:00:00+09:00",
                "occurred_at": "2031-03-05T10:00:00+09:00",
                "supersedes": None,
                "retracts": None,
                "request": None,
                "resolves": None,
                "blocked_by_request": None,
            }
        ],
    }


def case() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": "share-test-001",
        "channel": "share_text",
        "title": "상대 날짜로 잡은 회의",
        "reference": {"at": "2031-03-05T10:00:00+09:00", "timezone": "Asia/Seoul"},
        "tags": ["relative_date"],
        "input": {"stage": "spin", "text": TEXT, "counterpart": None},
        "known": {
            "entities": [
                {
                    "key": "seohyun",
                    "type": "person",
                    "name": "민서현",
                    "aliases": ["서현"],
                }
            ]
        },
        "expected": {
            "revise": {"policy": "keep"},
            "refine": {"select": [TEXT], "reject": []},
            "extract": {"alternatives": [extraction()]},
        },
    }


CALL = (
    "AI로 생성된 콘텐츠입니다\n\n예산 통화\n\n"
    "나  (00:01)\n여보세요 민서연 님?\n\n"
    "민서현  (00:04)\n네 내일 오후 두시에 회의 실 잡아 주세요\n"
)
REVISED = "네 내일 오후 두시에 회의실 잡아 주세요"


def call_case() -> dict[str, Any]:
    data = case()
    data.update(
        case_id="call-test-001",
        channel="call",
        tags=["stt_spacing"],
        input={"stage": "spin", "text": CALL, "counterpart": "민서현"},
    )
    data["expected"] = {
        "revise": {
            "policy": "revise",
            "edits": [
                {"from": "네 내일 오후 두시에 회의 실 잡아 주세요", "to": [REVISED]}
            ],
        },
        "refine": {"select": [REVISED], "reject": []},
        "extract": {"alternatives": []},
    }
    data["tags"] = ["stt_spacing", "empty"]
    return data


def test_minimal_case_resolves_keys_offsets_and_domain_models():
    loaded = validate_case(case())
    entity_id = loaded.entity_ids["seohyun"]
    assert ENT.match(entity_id)
    assert validate_case(case()).entity_ids == loaded.entity_ids  # deterministic
    assert loaded.known.entities[0].entity_id == entity_id
    assert loaded.revise_hints.entities[0].entity_id == entity_id
    [extracted] = loaded.extractions
    assert extracted.entities[0].entity_id == entity_id
    [candidate] = loaded.candidates
    assert (
        candidate.context
        == loaded.thread.text[candidate.start_char : candidate.end_char]
    )
    assert candidate.segment_ids == ["s1"]
    assert loaded.source.occurred_at == loaded.thread.occurred_at
    assert loaded.thread.text == TEXT  # text originals are never revised


def test_linked_entity_may_use_known_alias_and_tags_hold():
    data = case()
    data["tags"] = ["relative_date", "attendance", "new_entity"]
    first_extraction(data)["entities"][0]["name"] = "서현"
    first_extraction(data)["entities"][0]["aliases"] = ["서현"]
    assert validate_case(data).extractions[0].entities[0].name == "서현"


def test_gold_candidates_carry_rule_triggers_with_reference_time():
    loaded = validate_case(case())
    [candidate] = loaded.candidates
    assert {t.kind for t in candidate.triggers} >= {"date", "time"}
    assert all(t.reference_at == loaded.thread.occurred_at for t in candidate.triggers)
    # Kakao: the message time (not the save time) is the reference.
    [kakao] = [c for c in load_dataset(CASES_DIR) if c.case.case_id == "kakao-010"]
    [gold] = kakao.candidates
    [segment] = [s for s in kakao.thread.segments if s.segment_id in gold.segment_ids]
    assert gold.triggers and {t.reference_at for t in gold.triggers} == {
        segment.occurred_at
    }
    assert str(gold.triggers[0].reference_at).startswith("2031-03-04 23:50")
    gold.validate_thread(kakao.thread)


def test_call_edits_build_gold_revised_thread():
    loaded = validate_case(call_case())
    assert REVISED in loaded.thread.text
    assert "회의 실" in loaded.raw_thread.text and "회의 실" not in loaded.thread.text
    assert loaded.revise_hints.counterpart == "민서현"
    [candidate] = loaded.candidates
    assert candidate.context == REVISED
    # Revised coordinates differ from the original script's coordinates.
    assert loaded.raw_thread.text.find("네 내일") == loaded.thread.text.find("네 내일")
    assert len(loaded.thread.text) == len(loaded.raw_thread.text) - 1


def test_occurrence_selects_repeated_quote():
    data = case()
    text = "내일 보자. 내일 보자."
    data["input"]["text"] = text
    data["expected"]["refine"]["select"] = [{"text": "내일 보자.", "occurrence": 2}]
    data["expected"]["extract"] = {"alternatives": []}
    data["tags"] = ["empty"]
    [candidate] = validate_case(data).candidates
    assert candidate.start_char == 7


def mutate(change: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    data = case()
    change(data)
    return data


def first_extraction(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = data["expected"]["extract"]["alternatives"][0]
    return result


INVALID: dict[str, Callable[[dict[str, Any]], object]] = {
    "missing_version": lambda d: d.pop("schema_version"),
    "future_version": lambda d: d.update(schema_version=2),
    "string_version": lambda d: d.update(schema_version="1"),
    "extra_field": lambda d: d.update(extra=True),
    "missing_case_id": lambda d: d.pop("case_id"),
    "bad_case_id": lambda d: d.update(case_id="Share Test"),
    "voice_memo_channel": lambda d: d.update(channel="voice_memo"),
    "naive_reference": lambda d: d["reference"].update(at="2031-03-05T10:00:00"),
    "unknown_timezone": lambda d: d["reference"].update(timezone="Mars/Base"),
    "offset_not_timezone": lambda d: d["reference"].update(
        at="2031-03-05T10:00:00+00:00"
    ),
    "unknown_tag": lambda d: d.update(tags=["relative_date", "guess"]),
    "duplicate_tag": lambda d: d.update(tags=["relative_date", "relative_date"]),
    "missing_extract": lambda d: d["expected"].pop("extract"),
    "missing_revise_at_spin": lambda d: d["expected"].pop("revise"),
    "text_revised": lambda d: d["expected"].update(
        revise={"policy": "revise", "edits": []}
    ),
    "quote_absent": lambda d: d["expected"]["refine"].update(select=["없는 문장"]),
    "quote_ambiguous": lambda d: d["expected"]["refine"].update(select=["다"]),
    "quote_occurrence_absent": lambda d: d["expected"]["refine"].update(
        select=[{"text": TEXT, "occurrence": 2}]
    ),
    "overlapping_select": lambda d: d["expected"]["refine"].update(
        select=[TEXT, "예산 회의"]
    ),
    "reject_inside_select": lambda d: d["expected"]["refine"].update(
        reject=["예산 회의"]
    ),
    "excerpt_outside_candidates": lambda d: d["expected"]["refine"].update(
        select=["민서현 님이"]
    ),
    "facts_without_candidates": lambda d: d["expected"]["refine"].update(select=[]),
    "unknown_entity_key": lambda d: first_extraction(d)["entities"][0].update(
        entity_id="@nobody"
    ),
    "raw_entity_id": lambda d: first_extraction(d)["entities"][0].update(
        entity_id="ent_01ARZ3NDEKTSV4RRFFQ69G5FA0"
    ),
    "new_entity_absent_from_text": lambda d: first_extraction(d)["entities"][1].update(
        name="분기 결산"
    ),
    "dangling_ref": lambda d: first_extraction(d)["relations"][0].update(to="e9"),
    "invalid_kind": lambda d: first_extraction(d)["entry_drafts"][0].update(
        kind="메모"
    ),
    "foreign_offset": lambda d: first_extraction(d)["schedules"][0].update(
        start="2031-03-11T06:00:00+00:00"
    ),
    "empty_tag_with_answer": lambda d: d.update(tags=["empty"]),
    "missing_empty_tag": lambda d: d["expected"].update(extract={"alternatives": []}),
    "multi_without_tag": lambda d: d["expected"]["extract"]["alternatives"].append(
        extraction()
    ),
    "stt_tag_on_text": lambda d: d.update(tags=["relative_date", "stt_name"]),
    "duplicate_known_key": lambda d: d["known"]["entities"].append(
        dict(d["known"]["entities"][0])
    ),
    "not_object": lambda d: d.clear(),
    "linked_type_differs": lambda d: first_extraction(d)["entities"][0].update(
        type="org"
    ),
    "linked_name_differs": lambda d: first_extraction(d)["entities"][0].update(
        name="민서윤"
    ),
    "new_name_outside_excerpt": lambda d: first_extraction(d)["entities"][1].update(
        thread_excerpt="민서현 님이"
    ),
    "alias_outside_excerpt": lambda d: first_extraction(d)["entities"][1].update(
        aliases=["결산"]
    ),
    "short_excerpt": lambda d: first_extraction(d)["relations"][0].update(
        thread_excerpt="회의"
    ),
    "attendance_without_relation": lambda d: (
        d.update(tags=["relative_date", "attendance", "affiliation"]),
        first_extraction(d)["relations"][0].update(type="소속", to="e1"),
    ),
    "affiliation_without_relation": lambda d: d.update(
        tags=["relative_date", "affiliation"]
    ),
    "link_pending_without_ask": lambda d: d.update(
        tags=["relative_date", "link_pending"]
    ),
    "new_entity_tag_without_new": lambda d: (
        d.update(tags=["new_entity"]),
        first_extraction(d)["entities"][1].update(
            entity_id="@seohyun", name="서현", type="person"
        ),
    ),
    "request_tag_without_request": lambda d: d.update(
        tags=["relative_date", "request"]
    ),
    "cancel_tag_without_discard": lambda d: d.update(tags=["relative_date", "cancel"]),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_records_are_rejected(name):
    with pytest.raises(DatasetError):
        validate_case(mutate(INVALID[name]))


def test_multi_answer_tag_allows_alternatives_and_empty_alternative_is_explicit():
    data = case()
    other = extraction()
    other["entities"][1]["type"] = "topic"
    data["expected"]["extract"]["alternatives"].append(other)
    data["tags"] = ["relative_date", "multi_answer"]
    assert len(validate_case(data).extractions) == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["expected"].pop("revise"),
        lambda d: d["expected"]["revise"]["edits"][0].update(from_="x"),
        lambda d: d["expected"]["revise"]["edits"][0].update(**{"from": "없는 발화"}),
        lambda d: d["expected"]["revise"]["edits"][0].update(**{"from": "예산 통화"}),
        lambda d: d["expected"]["revise"].update(edits=[]),
        lambda d: d["expected"].update(revise={"policy": "keep"}),
        lambda d: (
            d.update(tags=["stt_spacing", "empty", "multi_answer"]),
            d["expected"]["revise"]["edits"][0]["to"].append("두 줄\n발화"),
        ),
    ],
)
def test_invalid_call_revisions_are_rejected(change):
    data = call_case()
    change(data)
    with pytest.raises(DatasetError):
        validate_case(data)


def test_later_stage_inputs_do_not_require_revise():
    data = case()
    data["input"]["stage"] = "extract"
    data["expected"].pop("revise")
    assert validate_case(data).case.input.stage == "extract"


def write(directory: Path, data: dict[str, Any], name: str | None = None) -> Path:
    path = directory / f"{name or data['case_id']}.json"
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def test_file_name_must_match_case_id_and_ids_are_unique(tmp_path):
    with pytest.raises(DatasetError, match="file name"):
        load_case(write(tmp_path, case(), "other"))
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(DatasetError, match="bad.json"):
        load_case(tmp_path / "bad.json")
    good = tmp_path / "good"
    good.mkdir()
    write(good, case())
    second = copy.deepcopy(case())
    second["case_id"] = "share-test-002"
    write(good, second)
    assert [c.case.case_id for c in load_dataset(good)] == [
        "share-test-001",
        "share-test-002",
    ]
    with pytest.raises(DatasetError, match="no cases"):
        load_dataset(tmp_path / "missing")


def test_public_dataset_meets_minimums_and_is_reviewable():
    cases = load_dataset(CASES_DIR)
    assert len(cases) >= 30
    ids = [c.case.case_id for c in cases]
    assert len(ids) == len(set(ids))
    counts = summarize(cases)
    assert counts["total"] == len(cases)
    assert counts["channel"]["kakao"] >= 10
    stt = [
        c
        for c in cases
        if c.case.channel == "call" and any(t.startswith("stt_") for t in c.case.tags)
    ]
    assert len(stt) >= 10
    assert counts["channel"]["share_text"] >= 1
    required = {
        "pseudonym",
        "homonym",
        "alias",
        "relative_date",
        "timezone",
        "cancel",
        "reschedule",
        "chitchat",
        "missing_info",
        "link_pending",
        "relation_direction",
        "affiliation",
        "empty",
        "multi_answer",
        "boundary",
    }
    assert required <= set(counts["tag"])
    assert counts["answer"]["empty"] >= 1 and counts["answer"]["ask"] >= 1
    assert set(counts["entry_kind"]) == {
        "요청",
        "논의",
        "결정",
        "폐기",
        "약속",
        "일정",
        "변경",
    }
    assert {"참석", "소속"} <= set(counts["relation_type"])
    for loaded in cases:
        for extracted in loaded.extractions:
            for entity in extracted.entities:
                if entity.entity_id is not None:
                    assert entity.entity_id in loaded.entity_ids.values()


def test_public_samples_are_fake_only():
    # Fake data uses the reserved 2030s dates and no phone/account-like digits.
    for path in sorted(CASES_DIR.glob("*.json")):
        raw = path.read_text()
        assert not re.search(r"01[016789]-?\d{3,4}-?\d{4}", raw), path.name
        assert not re.search(r"\d{6}-\d{7}", raw), path.name
        assert not re.search(r"(?<!\d)0[2-6]\d?-\d{3,4}-\d{4}(?!\d)", raw), path.name
        accounts = re.findall(r"(?<![\d-])\d{2,6}-\d{2,6}-\d{2,6}(?![\d-])", raw)
        assert all(re.fullmatch(r"203\d-\d{2}-\d{2}", a) for a in accounts), path
        data = json.loads(raw)
        assert "@" not in data["input"]["text"], path.name
        for text in strings(data):
            if re.fullmatch(TIMESTAMP_PATTERN, text):
                assert text.startswith("203"), path.name
        assert all(y.startswith("203") for y in re.findall(r"(\d{4})\s*년", raw))


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    return []
