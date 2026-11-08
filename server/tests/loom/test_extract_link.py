"""Candidate-only extraction with a fake LlmClient; no network or user storage."""

import copy
import json
from pathlib import Path

import anthropic
import pytest
from pydantic import ValidationError

from weaver.config import RefinerConfig
from weaver.llm.errors import InvalidJson
from weaver.loom.extract import ExtractionOutput, LlmExtractor, matching_entities
from weaver.loom.refine import RuleRefiner
from weaver.schema import Source, Thread
from weaver.schema.internal import EntityIndex

ROOT = Path(__file__).resolve().parents[3]
ID1 = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA0"
ID2 = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA1"
ID3 = "ent_01ARZ3NDEKTSV4RRFFQ69G5FA2"
TEXT = "김민수님이 내일 오후 3시 점검 회의를 요청합니다."


def entity(entity_id=ID1, name="김민수", aliases=None, kind="person"):
    return dict(entity_id=entity_id, name=name, aliases=aliases or [], type=kind)


def index(*entities):
    return EntityIndex.model_validate(dict(entities=list(entities)))


def response():
    data = json.loads((ROOT / "schema/examples/Proposal.json").read_text())
    for key in ["schema_version", "proposal_id", "rev", "source"]:
        del data[key]
    data["thread_excerpt"] = TEXT
    for item in [
        *data["entities"],
        *data["schedules"],
        *data["relations"],
        *data["entry_drafts"],
    ]:
        item.update(thread_excerpt=TEXT, ask=None)
    data["entities"][0].update(entity_id=ID1, aliases=[])
    data["entities"][1].update(entity_id=None)
    data["relations"][0]["reason"] = "김민수가 점검 회의를 요청했다."
    return data


class FakeLlm:
    def __init__(self, data=None, error=None):
        self.data = response() if data is None else data
        self.error = error
        self.calls = []

    def generate_json(self, task, system, user, schema):
        self.calls.append((task, system, json.loads(user), schema))
        if self.error:
            raise self.error
        # Simulate LlmClient's JSON decoding plus full Pydantic validation.
        wire = copy.deepcopy(self.data)
        for draft in wire["entry_drafts"]:
            draft["value"] = json.dumps(draft["value"], ensure_ascii=False)
        return schema.model_validate_json(json.dumps(wire))

    def generate_text(self, *args):
        raise AssertionError("extractor must only use generate_json")


def run(data=None, known=None, text=TEXT):
    thread = Thread(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        channel="kakao",
        occurred_at="2026-11-07T23:30:00+09:00",
        text=text,
        segments=[],
    )
    fake = FakeLlm(data)
    result = LlmExtractor(fake).extract(
        thread,
        RuleRefiner(RefinerConfig(before=0, after=0)).select(thread),
        known if known is not None else index(entity()),
    )
    return result, fake


def test_empty_candidates_never_call_llm():
    fake = FakeLlm()
    thread = Thread(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        channel="kakao",
        occurred_at=None,
        text="안녕하세요.",
        segments=[],
    )
    extraction = LlmExtractor(fake).extract(thread, [], index())
    assert not fake.calls
    assert extraction is None


def test_candidate_only_payload_and_approved_snapshot_are_read_only():
    known = index(entity(), entity(ID3, "민감한비후보이름"))
    before = known.model_dump()
    result, fake = run(known=known, text="비공개본문. " + TEXT + " 숨겨진끝문장.")
    assert len(fake.calls) == 1
    task, system, payload, schema = fake.calls[0]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "비공개본문" not in serialized and "숨겨진끝문장" not in serialized
    assert "민감한비후보이름" not in serialized
    assert payload["known"]["entities"] == [entity()]
    assert (
        payload["candidates"][0]["triggers"][0]["reference_at"]
        == "2026-11-07T23:30:00+09:00"
    )
    assert payload["timezone"] == "Asia/Seoul"
    assert task == "loom.extract" and schema is ExtractionOutput
    assert "untrusted" in system and "relative dates" in system
    assert known.model_dump() == before
    assert result.entities[0].entity_id == ID1


def test_normalization_preserves_all_name_and_alias_collisions():
    known = index(
        entity(name="ＡＬＩＣＥ  Kim"), entity(ID2, "다른 사람", ["alice kim"])
    )
    assert len(matching_entities("내일 Alice\tKim님과 만남", known)) == 2


@pytest.mark.parametrize("alias", [False, True])
def test_same_name_or_shared_title_requires_ask(alias):
    known = index(
        entity(aliases=["팀장님"]),
        entity(ID2, "이영희" if alias else "김민수", ["팀장님"] if alias else []),
    )
    data = response()
    if alias:
        text = TEXT.replace("김민수님", "팀장님")
        data["thread_excerpt"] = text
        for items in [
            data["entities"],
            data["schedules"],
            data["relations"],
            data["entry_drafts"],
        ]:
            for item in items:
                item["thread_excerpt"] = text
    else:
        text = TEXT
    result, fake = run(data, known, text)
    assert len(fake.calls[0][2]["known"]["entities"]) == 2
    assert result.entities[0].entity_id is None
    assert {c.entity_id for c in result.entities[0].ask.choices} == {ID1, ID2}


def test_unique_alias_is_never_an_automatic_identity_confirmation():
    data = response()
    data["entities"][0]["name"] = "김민수"
    known = index(entity(name="김길동", aliases=["김민수"]))
    result, _ = run(data, known)
    assert result.entities[0].entity_id is None
    assert result.entities[0].ask.choices[0].entity_id == ID1


@pytest.mark.parametrize(
    "collection", ["entities", "schedules", "relations", "entry_drafts"]
)
@pytest.mark.parametrize("confidence", [0.0, 0.799, 0.8])
def test_confidence_threshold_for_every_item(collection, confidence):
    data = response()
    data[collection][0]["confidence"] = confidence
    result, _ = run(data)
    item = getattr(result, collection)[0]
    assert (item.ask is not None) == (confidence < 0.8)
    if collection == "entities":
        assert (item.entity_id is None) == (confidence < 0.8)


def test_new_topic_and_project_remain_distinct():
    data = response()
    data["entities"][0].update(entity_id=None, name="김민수", type="topic")
    result, _ = run(data, index())
    assert [(e.type, e.entity_id) for e in result.entities] == [
        ("topic", None),
        ("project", None),
    ]


def test_relative_date_relation_reason_request_and_ask_survive_proposal():
    data = response()
    data["entities"][0]["confidence"] = 0.6
    data["entities"][0]["ask"] = dict(question="이 사람이 맞나요?", choices=[])
    # Exercise all legal direct-ID locations when identity is deferred.
    data["entry_drafts"][0]["entities"][0] = ID1
    data["entry_drafts"][0]["request"]["requester"] = ID1
    data["entry_drafts"][0]["request"]["targets"] = [ID1, "e2"]
    data["schedules"][0]["entities"][0] = ID1
    data["relations"][0]["from"] = ID1
    result, _ = run(data)
    source = Source.model_validate_json(
        (ROOT / "schema/examples/Source.json").read_text()
    )
    proposal = result.to_proposal(source, "prp_01ARZ3NDEKTSV4RRFFQ69G5FA0", 1)
    assert proposal.entities[0].ask.question == "이 사람이 맞나요?"
    assert proposal.entities[0].entity_id is None
    assert proposal.entry_drafts[0].request.requester == "e1"
    assert proposal.entry_drafts[0].request.targets == ["e1", "e2"]
    assert proposal.entry_drafts[0].entities[0] == "e1"
    assert proposal.schedules[0].entities[0] == "e1"
    assert proposal.relations[0].from_ref == "e1"
    assert proposal.relations[0].reason == data["relations"][0]["reason"]
    assert proposal.relations[0].thread_excerpt == TEXT
    assert proposal.schedules[0].start.isoformat() == "2026-11-08T15:00:00+09:00"
    assert proposal.schedules[0].blocked_by_request == "d1"
    assert all(d.resolves is None for d in proposal.entry_drafts)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_id",
        "unselected_id",
        "broken_ref",
        "duplicate_ref",
        "missing_ref",
        "type",
        "kind",
        "request",
        "block",
        "schedule_block",
        "resolves",
        "evidence",
        "choice",
        "empty",
        "reason",
    ],
)
def test_invalid_output_rejects_whole_extraction(mutation):
    data = response()
    known = index(entity(), entity(ID3, "非候補"))
    if mutation == "unknown_id":
        data["entities"][0]["entity_id"] = ID2
    elif mutation == "unselected_id":
        data["entities"][0]["entity_id"] = ID3
    elif mutation == "broken_ref":
        data["entry_drafts"][0]["entities"] = ["e9"]
    elif mutation == "duplicate_ref":
        data["entities"][0]["ref"] = "d1"
    elif mutation == "missing_ref":
        data["entities"][0]["ref"] = None
    elif mutation == "type":
        data["entities"][0]["type"] = "place"
    elif mutation == "kind":
        data["entry_drafts"][0]["kind"] = "unknown"
    elif mutation == "request":
        data["entry_drafts"][0]["request"] = None
    elif mutation == "block":
        data["entry_drafts"][1]["blocked_by_request"] = "d9"
    elif mutation == "schedule_block":
        data["schedules"][0]["blocked_by_request"] = None
    elif mutation == "resolves":
        data["entry_drafts"][1]["resolves"] = "evt_01ARZ3NDEKTSV4RRFFQ69G5FA0"
    elif mutation == "evidence":
        data["relations"][0]["thread_excerpt"] = "후보 밖 문장"
    elif mutation == "choice":
        data["entities"][0]["ask"] = dict(
            question="누구?", choices=[dict(entity_id=ID2, label="미등록")]
        )
    elif mutation == "empty":
        data["summary"] = "   "
    elif mutation == "reason":
        data["relations"][0]["reason"] = None
    with pytest.raises((ValueError, ValidationError)):
        run(data, known)


def test_invalid_json_error_is_propagated_without_retry():
    fake = FakeLlm(error=InvalidJson())
    thread = Thread(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        channel="kakao",
        occurred_at=None,
        text=TEXT,
        segments=[],
    )
    with pytest.raises(InvalidJson):
        LlmExtractor(fake).extract(
            thread, RuleRefiner().select(thread), index(entity())
        )
    assert len(fake.calls) == 1


def test_simplified_schema_is_provider_portable_and_full_validation_survives():
    schema = ExtractionOutput.model_json_schema()
    serialized = json.dumps(schema)
    for unsupported in ['"allOf"', '"if"', '"then"', r"\\S"]:
        assert unsupported not in serialized
    wire = anthropic.transform_schema(ExtractionOutput)
    assert wire["type"] == "object"
    assert '"allOf"' not in json.dumps(wire)
    data = response()
    data["entry_drafts"][0]["request"] = None
    with pytest.raises(ValidationError):
        ExtractionOutput.model_validate(data)


def test_bad_candidate_is_rejected_before_call():
    thread = Thread(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        channel="kakao",
        occurred_at=None,
        text=TEXT,
        segments=[],
    )
    candidates = RuleRefiner().select(thread)
    candidates[0].context = "틀린 문맥"
    fake = FakeLlm()
    with pytest.raises(ValueError, match="context"):
        LlmExtractor(fake).extract(thread, candidates, index())
    assert not fake.calls


def test_repeated_deferred_id_with_direct_reference_is_not_guessed():
    data = response()
    extra = copy.deepcopy(data["entities"][0])
    extra.update(ref="e3", confidence=0.5)
    data["entities"][0]["confidence"] = 0.5
    data["entities"].append(extra)
    data["entry_drafts"][0]["entities"] = [ID1]
    with pytest.raises(ValueError, match="ambiguous"):
        run(data)


@pytest.mark.parametrize(
    "value",
    [None, False, 1, 1.5, "문자열", [1, {"한글": True}], {"임의 키": [None, "값"]}],
)
def test_provider_json_string_restores_every_v1_value_type(value):
    data = response()
    data["entry_drafts"][0]["value"] = value
    result, _ = run(data)
    assert result.entry_drafts[0].value == value
    assert type(result.entry_drafts[0].value) is type(value)


def test_null_id_alias_collision_keeps_all_choices():
    data = response()
    data["entities"][0]["entity_id"] = None
    known = index(entity(aliases=["회의"]), entity(ID2, "이영희", ["회의"]))
    result, _ = run(data, known)
    assert {c.entity_id for c in result.entities[0].ask.choices} == {ID1, ID2}


def test_explicit_identity_question_defers_even_high_confidence_link():
    data = response()
    data["entities"][0]["ask"] = dict(question="김민수가 이 사람이 맞나요?", choices=[])
    result, _ = run(data)
    assert result.entities[0].entity_id is None
    assert result.entities[0].ask.question == "김민수가 이 사람이 맞나요?"


def test_extractor_works_through_real_client_with_fake_transport(tmp_path):
    from weaver.config import LlmConfig
    from weaver.llm.client import AnthropicClient
    from weaver.llm.ledger import Ledger
    from weaver.llm.types import BackendReply

    class Transport:
        def __init__(self):
            self.requests = []

        async def send(self, request, api_key):
            self.requests.append(request)
            wire = response()
            for draft in wire["entry_drafts"]:
                draft["value"] = json.dumps(draft["value"])
            return BackendReply(text=json.dumps(wire))

    transport = Transport()
    client = AnthropicClient(
        LlmConfig(backend="anthropic", api_key_env="FAKE_TEST_KEY"),
        Ledger(tmp_path / "ledger.sqlite3"),
        transport=transport,
        environ={"FAKE_TEST_KEY": "fake"},
    )
    thread = Thread(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        channel="kakao",
        occurred_at=None,
        text=TEXT,
        segments=[],
    )
    extraction = LlmExtractor(client, timezone="UTC").extract(
        thread, RuleRefiner().select(thread), index(entity())
    )
    assert extraction.entities[0].entity_id == ID1
    assert len(transport.requests) == 1
    assert json.loads(transport.requests[0].user)["timezone"] == "UTC"
    assert transport.requests[0].json_schema["type"] == "object"


def rename_evidence(data, text):
    data["thread_excerpt"] = text
    for collection in ["entities", "schedules", "relations", "entry_drafts"]:
        for item in data[collection]:
            item["thread_excerpt"] = text


def test_review_mismatched_name_does_not_link_to_other_person_in_excerpt():
    data = response()
    text = "김철수님이 김민수님에게 내일 회의를 요청합니다."
    rename_evidence(data, text)
    data["entities"][0]["name"] = "김철수"
    result, _ = run(data, index(entity()), text)
    assert result.entities[0].entity_id is None
    assert result.entities[0].ask is not None


def test_review_longer_name_never_confirms_shorter_name_id():
    data = response()
    text = "이준호님이 내일 회의를 요청합니다."
    rename_evidence(data, text)
    data["entities"][0]["name"] = "이준호"
    # A short name not actually mentioned is omitted, making this ID invalid.
    with pytest.raises(ValueError, match="unselected"):
        run(data, index(entity(name="이준")), text)


@pytest.mark.parametrize(
    "collection", ["entities", "schedules", "relations", "entry_drafts"]
)
def test_review_choice_label_is_generated_from_index(collection):
    data = response()
    data[collection][0]["ask"] = dict(
        question="누구인가요?", choices=[dict(entity_id=ID1, label="박영희")]
    )
    result, _ = run(data)
    label = getattr(result, collection)[0].ask.choices[0].label
    assert "김민수" in label and "박영희" not in label


def test_review_same_name_choices_are_distinguishable_from_existing_data():
    known = index(entity(aliases=["개발팀장"]), entity(ID2, aliases=["인사팀장"]))
    result, _ = run(known=known)
    choices = result.entities[0].ask.choices
    assert len({c.label for c in choices}) == 2
    assert any("개발팀장" in c.label for c in choices)
    assert any("인사팀장" in c.label for c in choices)
    assert known.entities[0].aliases == ["개발팀장"]


@pytest.mark.parametrize("field", ["supersedes", "retracts"])
def test_review_ai_cannot_invent_durable_entry_ids(field):
    data = response()
    data["entry_drafts"][0][field] = "evt_01ARZ3NDEKTSV4RRFFQ69G5FA0"
    with pytest.raises(ValueError, match=field):
        run(data)


def test_review_unblocked_items_with_request_require_question():
    data = response()
    data["schedules"][0]["blocked_by_request"] = None
    data["entry_drafts"][1]["blocked_by_request"] = None
    result, _ = run(data)
    for item in [result.schedules[0], result.entry_drafts[1]]:
        assert item.ask.question == "요청에서 나온 항목인가요?"
    assert result.entry_drafts[0].ask is None


def test_review_request_question_preserves_existing_question():
    data = response()
    data["schedules"][0]["blocked_by_request"] = None
    data["entry_drafts"][1]["blocked_by_request"] = None
    data["entry_drafts"][1]["ask"] = dict(question="시각이 맞나요?", choices=[])
    result, _ = run(data)
    assert "시각이 맞나요?" in result.entry_drafts[1].ask.question
    assert "요청에서 나온 항목인가요?" in result.entry_drafts[1].ask.question


@pytest.mark.parametrize(
    "text",
    [
        "이준호님",
        "김이준",
        "이준호님이",
        "전이준",
        "이준회의",
        "AIModel",
        "preAI",
        "회의실",
    ],
)
def test_review_short_name_or_alias_is_not_a_substring_match(text):
    assert not matching_entities(
        text, index(entity(name="이준", aliases=["AI", "회의"]))
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "님",
        "씨",
        "님이",
        "님께",
        "씨에게",
        "은",
        "가",
        "와",
        "에게는",
        "으로",
        "에서",
        "한테",
        "도",
    ],
)
def test_review_korean_name_boundaries_allow_honorifics_and_particles(suffix):
    assert matching_entities(
        f"오늘 이준{suffix} 내일 만나요.", index(entity(name="이준"))
    )


def test_review_long_name_mention_defers_selected_short_name_id():
    data = response()
    text = "이준님과 이준호님이 내일 회의를 요청합니다."
    rename_evidence(data, text)
    data["entities"][0].update(name="이준호", thread_excerpt="이준호님")
    result, _ = run(data, index(entity(name="이준")), text)
    assert result.entities[0].entity_id is None
    assert result.entities[0].ask is not None


def test_review_choice_id_suffix_expands_when_last_six_characters_collide():
    other_id = "ent_11ARZ3NDEKTSV4RRFFQ69G5FA0"
    result, _ = run(known=index(entity(), entity(other_id)))
    labels = [c.label for c in result.entities[0].ask.choices]
    assert len(set(labels)) == 2


def test_review_request_heuristic_does_not_set_execution_block():
    data = response()
    data["entry_drafts"][1]["blocked_by_request"] = None
    data["schedules"][0]["blocked_by_request"] = None
    result, _ = run(data)
    source = Source.model_validate_json(
        (ROOT / "schema/examples/Source.json").read_text()
    )
    proposal = result.to_proposal(source, "prp_01ARZ3NDEKTSV4RRFFQ69G5FA0", 1)
    assert proposal.schedules[0].blocked_by_request is None
    assert proposal.schedules[0].ask.question == "요청에서 나온 항목인가요?"
    assert proposal.entry_drafts[1].blocked_by_request is None
    assert all(d.resolves is None for d in proposal.entry_drafts)
