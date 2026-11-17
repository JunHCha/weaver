"""Synthetic PARA and counterpart guards at the real extraction boundary."""

import json
from datetime import UTC, datetime

import pytest

from weaver.context import ContextDictionary, Page
from weaver.llm.errors import SchemaMismatch
from weaver.loom.summary_review import ApprovalExtractor
from weaver.schema import ReviewPackage, Verdict

NOW = datetime(2032, 1, 1, tzinfo=UTC)
SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"


class Llm:
    def __init__(self, tags, counterpart=None, additions=()):
        self.tags, self.counterpart, self.additions = tags, counterpart, additions
        self.payload = {}

    def generate_json(self, task, system, user, schema):
        self.payload = json.loads(user)
        for rule in (
            "정확히 하나",
            "기존 영역을 우선",
            "새 project는 만들지 않는다",
            "counterpart",
            "받음 요청의 요청자",
            "보냄 요청의 수신자",
        ):
            assert rule in system
        return schema.model_validate(
            {
                "body": "김철수가 자료를 전달했다.",
                "new_entries": self.additions,
                "corrections": [],
                "threads": [
                    {
                        "kind": "기록",
                        "content": "김철수가 자료를 전달했다.",
                        "tags": self.tags,
                        "counterpart": self.counterpart,
                        "occurred_at": NOW.isoformat(),
                        "field": None,
                        "value": None,
                        "direction": None,
                        "resolves": None,
                        "retracts": None,
                    }
                ],
            }
        )


def prepare(context, llm):
    package = ReviewPackage(
        schema_version=2,
        package_id="para",
        source_id=SOURCE,
        revision=1,
        draft_body="김철수가 자료를 전달했다.",
        questions=[],
        stage="검토",
        stage_times={},
    )
    verdict = Verdict(
        schema_version=2,
        package_id="para",
        revision=1,
        action="approve",
        answers=[],
        edited_body=None,
        feedback=None,
    )
    return ApprovalExtractor(
        llm, context, object(), contact_name="김철수", source_occurred_at=NOW
    ).prepare(package, package.draft_body, verdict, now=NOW)


def test_existing_area_curated_project_and_canonical_counterpart(tmp_path, caplog):
    context = ContextDictionary(tmp_path)
    context.sync_from_pages(
        [Page("자동 사건", "project"), Page("사람이 고른 사업", "project")]
    )
    context.set_kind("사람이 고른 사업", "project")
    context.add_entry("자료 관리", "area")
    context.add_entry("김철수", "person", ("김 박사",))
    llm = Llm(
        ["자료 관리", "사람이 고른 사업", "자동 사건", "새 사업"],
        "김 박사",
        [
            {"name": "새 사업", "kind": "project"},
            {"name": "자동 사건", "kind": "topic"},
            {"name": "새 자료", "kind": "topic", "aliases": ["자동 사건"]},
        ],
    )
    result = prepare(context, llm)
    assert result.threads[0].tags == ["자료 관리", "사람이 고른 사업", "김철수"]
    assert result.threads[0].counterpart == "김철수"
    assert result.new_entries == []
    assert "자동 사건" not in {e["name"] for e in llm.payload["dictionary"]}
    assert "dropped automatic project proposal" in caplog.text
    assert "새 사업" not in caplog.text


@pytest.mark.parametrize("tags", [[], ["자료 관리", "외부 협력"]])
def test_exactly_one_area_required(tmp_path, tags, caplog):
    context = ContextDictionary(tmp_path)
    for name in ("자료 관리", "외부 협력"):
        context.add_entry(name, "area")
    with pytest.raises(SchemaMismatch):
        prepare(context, Llm(tags))
    assert "invalid business area tag count" in caplog.text
    assert "자료 관리" not in caplog.text


@pytest.mark.parametrize("counterpart", [None, "자료 관리", "없는 상대"])
def test_counterpart_null_or_person_organization_only(tmp_path, counterpart, caplog):
    context = ContextDictionary(tmp_path)
    context.add_entry("자료 관리", "area")
    result = prepare(context, Llm(["자료 관리"], counterpart))
    assert result.threads[0].counterpart is None
    assert ("dropped invalid business counterpart" in caplog.text) == (
        counterpart is not None
    )


def test_new_area_and_counterpart_only_entry_are_kept(tmp_path):
    context = ContextDictionary(tmp_path)
    llm = Llm(
        ["자료 관리"],
        "가상연구소",
        [
            {"name": "자료 관리", "kind": "area"},
            {"name": "가상연구소", "kind": "organization"},
        ],
    )
    result = prepare(context, llm)
    assert {e.name for e in result.new_entries} == {"자료 관리", "가상연구소"}
    assert result.threads[0].counterpart == "가상연구소"


def test_counterpart_is_added_to_tags_and_not_warned_as_untagged(tmp_path, caplog):
    context = ContextDictionary(tmp_path)
    context.add_entry("資料管理", "area")
    context.add_entry("김철수", "person", ("김 박사",))
    result = prepare(context, Llm(["資料管理"], "김 박사"))
    assert result.threads[0].tags == ["資料管理", "김철수"]
    assert "untagged dictionary names" not in caplog.text
