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


# --- path tree mode ---------------------------------------------------------


def tree_context(tmp_path, extra=()):
    from weaver.context.tree import expand

    context = ContextDictionary(tmp_path)
    context.sync_tree(
        "회사",
        expand(
            "회사",
            [
                "회사/infra/network/router",
                "회사/project/26-12-라우터-교체",
                "회사/project/26-13-다른-작업",
                *extra,
            ],
        ),
    )
    context.add_entry("홍길동", "person")
    return context


def decide(context, values, **kwargs):
    from weaver.loom.tagging import business_tags

    return business_tags(context.snapshot(), [], {}, values, **kwargs)


def test_tree_subject_project_and_mentions(tmp_path, caplog):
    result = decide(
        tree_context(tmp_path),
        [
            "회사/infra/network/router",
            "회사/project/26-12-라우터-교체",
            "홍길동",
            "네트워크작업",
        ],
    )
    assert result.tags == [
        "회사/infra/network/router",
        "회사/project/26-12-라우터-교체",
        "홍길동",
    ]
    assert result.unknown_paths == []
    assert result.fallback is None
    assert "dropped free tag" in caplog.text


def test_unknown_path_and_free_words_fall_back(tmp_path):
    result = decide(tree_context(tmp_path), ["회사/infra/storage/nas", "가끔"])
    assert result.tags == ["회사/미분류"]
    assert result.unknown_paths == ["회사/infra/storage/nas"]
    assert result.fallback == "회사/미분류"


def test_limits(tmp_path, caplog):
    context = tree_context(tmp_path, ["회사/infra/network/switch", "회사/stacks/go"])
    result = decide(
        context,
        [
            "회사/infra/network/router",
            "회사/infra/network/switch",
            "회사/stacks/go",
            "회사/project/26-12-라우터-교체",
            "회사/project/26-13-다른-작업",
        ],
    )
    assert result.tags == [
        "회사/infra/network/router",
        "회사/infra/network/switch",
        "회사/project/26-12-라우터-교체",
    ]
    assert "dropped extra" in caplog.text


def test_no_path_uses_default_sphere(tmp_path):
    result = decide(tree_context(tmp_path), ["홍길동"], default_sphere="가족")
    assert result.tags == ["가족/미분류", "홍길동"]


def test_namespace_and_root_are_not_subjects(tmp_path):
    context = tree_context(tmp_path)
    for value in ("회사/infra", "회사"):
        assert decide(context, [value]).fallback == "회사/미분류"


def test_bare_root_or_namespace_tag_names_the_fallback_sphere(tmp_path):
    context = tree_context(tmp_path, ["가족/건강"])
    assert decide(context, ["가족"]).tags == ["가족/미분류"]
    assert decide(context, ["가족"]).fallback == "가족/미분류"
    assert decide(context, ["가족", "홍길동"]).tags == ["가족/미분류", "홍길동"]
    # an unknown path still wins over a bare root
    result = decide(context, ["가족", "회사/infra/storage/nas"], default_sphere="가족")
    assert result.fallback == "회사/미분류"


def test_explicit_unclassified_path_is_kept(tmp_path):
    context = tree_context(tmp_path, ["가족/미분류"])
    result = decide(context, ["가족/미분류", "홍길동"])
    assert result.tags == ["가족/미분류", "홍길동"]
    assert result.fallback is None


def test_unclassified_nodes_survive_a_tight_path_budget(tmp_path, monkeypatch):
    from weaver.loom import tagging

    context = tree_context(tmp_path, ["가족/건강", "가족/미분류"])
    monkeypatch.setattr(tagging, "PATH_BUDGET", 120)
    paths = tagging.tag_context(context.snapshot(), "스크립트", [])["paths"]
    assert "가족/미분류" in paths and "회사/미분류" in paths
    assert tagging.TAG_RULES.count("미분류") >= 1


def test_legacy_mode_returns_decision_without_unknowns(tmp_path):
    context = ContextDictionary(tmp_path)
    context.add_entry("자료 관리", "area")
    result = decide(context, ["자료 관리"])
    assert (result.tags, result.unknown_paths, result.fallback) == (
        ["자료 관리"],
        [],
        None,
    )


def test_tree_node_new_entries_dropped(tmp_path, caplog):
    from weaver.loom.tagging import NewEntry, proposals

    added, names = proposals(
        tree_context(tmp_path).snapshot(),
        [
            NewEntry(name="회사/infra/x", kind="topic"),
            NewEntry(name="박철수", kind="person"),
        ],
    )
    assert [e.name for e in added] == ["박철수"]
    assert "dropped tree node proposal" in caplog.text
    assert (
        proposals(
            tree_context(tmp_path / "b").snapshot(),
            [NewEntry(name="회사/infra/x", kind="topic")],
        )[0]
        == []
    )


def test_tag_context_paths_bounded(tmp_path):
    from weaver.context.tree import expand
    from weaver.loom.tagging import tag_context

    context = ContextDictionary(tmp_path)
    context.sync_tree(
        "회사",
        expand(
            "회사",
            [
                *(f"회사/infra/network/장비-{i:04d}" for i in range(2000)),
                "회사/infra/network/router",
            ],
        ),
    )
    context.add_entry("홍길동", "person")
    hints = tag_context(context.snapshot(), "router 교체", [])
    assert len(json.dumps(hints["paths"], ensure_ascii=False).encode()) <= 4000
    assert "회사/infra/network/router" in hints["paths"]
    assert "회사/infra/network" in hints["paths"]
    assert [e["name"] for e in hints["dictionary"]] == ["홍길동"]
