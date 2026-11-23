from pathlib import Path

import pytest

from weaver.context import ContextDictionary, ContextEntry, DictionarySnapshot
from weaver.context.tree import (
    TreeNode,
    area_label,
    area_of,
    expand,
    home_path,
    node_kind,
    root_of,
)


@pytest.mark.parametrize(
    ("path", "kind", "area"),
    [
        ("회사", "area", None),
        ("회사/미분류", "area", "회사/미분류"),
        ("회사/infra", "topic", None),
        ("회사/infra/network", "area", "회사/infra/network"),
        ("회사/infra/network/router", "topic", "회사/infra/network"),
        ("회사/domain", "topic", None),
        ("회사/domain/인사제도", "area", "회사/domain/인사제도"),
        ("회사/domain/인사제도/x", "topic", "회사/domain/인사제도"),
        ("회사/stacks", "topic", None),
        ("회사/stacks/python", "topic", None),
        ("회사/project", "topic", None),
        ("회사/project/alpha", "project", None),
        ("회사/project/alpha/sub", "project", None),
        ("가족/golf", "area", "가족/golf"),
        ("가족/golf/swing", "topic", "가족/golf"),
        ("가족/미분류", "area", "가족/미분류"),
    ],
)
def test_node_kind_and_area_of(path: str, kind: str, area: str | None) -> None:
    assert node_kind(path) == kind
    assert area_of(path) == area
    assert root_of(path) == path.split("/")[0]


def test_area_label() -> None:
    assert area_label("회사/infra/network") == "Network"
    assert area_label("회사/domain/인사제도") == "인사제도"


def test_expand_adds_ancestors_sphere_and_unclassified() -> None:
    nodes = expand("회사", ["회사/infra/network/router"])
    assert nodes == [
        TreeNode("회사", "area"),
        TreeNode("회사/infra", "topic"),
        TreeNode("회사/infra/network", "area"),
        TreeNode("회사/infra/network/router", "topic"),
        TreeNode("회사/미분류", "area"),
    ]
    assert expand("가족", []) == [
        TreeNode("가족", "area"),
        TreeNode("가족/미분류", "area"),
    ]


def test_home_path_para_mapping() -> None:
    assert home_path("가족", "3. Resource/golf") == "가족/golf"
    assert home_path("부업", "1. Project/수련회") == "부업/project/수련회"
    assert home_path("가족", "2. areas/건강/운동") == "가족/건강/운동"
    assert home_path("가족", "4. Archive/x") is None
    assert home_path("가족", "") == "가족"


def _nodes(sphere: str, *names: str) -> list[TreeNode]:
    return [TreeNode(n, node_kind(n)) for n in names]


def test_sync_tree_replaces_one_sphere(tmp_path: Path) -> None:
    dictionary = ContextDictionary(tmp_path)
    dictionary.sync_tree(
        "회사", _nodes("회사", "회사", "회사/미분류", "회사/infra", "회사/old")
    )
    dictionary.sync_tree("가족", _nodes("가족", "가족", "가족/미분류"))
    dictionary.sync_tree("회사", _nodes("회사", "회사", "회사/미분류", "회사/new"))
    names = {e.name for e in dictionary.list_entries() if e.source == "tree"}
    assert names == {"가족", "가족/미분류", "회사", "회사/미분류", "회사/new"}


def test_tree_entries_resolve_and_are_readonly(tmp_path: Path) -> None:
    dictionary = ContextDictionary(tmp_path)
    dictionary.sync_tree("회사", expand("회사", ["회사/infra/network/router"]))
    entry = dictionary.snapshot().resolve("회사/infra/network/router")
    assert entry is not None and entry.source == "tree"
    dictionary.add_entry("다른", "topic")
    for call in (
        lambda: dictionary.rename("회사/infra", "x"),
        lambda: dictionary.set_kind("회사/infra", "area"),
        lambda: dictionary.add_alias("회사/infra", "인프라"),
        lambda: dictionary.merge("다른", "회사/infra"),
        lambda: dictionary.merge("회사/infra", "다른"),
    ):
        with pytest.raises(ValueError, match="tree node is read-only"):
            call()
    readonly = ContextDictionary.open_readonly(tmp_path / "context.sqlite3")
    found = readonly.snapshot().resolve("회사/infra/network")
    assert found is not None and found.source == "tree"
    assert dictionary.snapshot().has_tree


def test_tree_sync_does_not_taint_policy_lineage(tmp_path: Path) -> None:
    dictionary = ContextDictionary(tmp_path)
    before = dictionary.policy_origins()
    dictionary.sync_tree("회사", expand("회사", ["회사/infra"]))
    assert dictionary.policy_origins() == before


def test_area_of_snapshot() -> None:
    snapshot = DictionarySnapshot(
        [
            ContextEntry("회사/infra/network", "area", source="tree"),
            ContextEntry("회사/infra/network/router", "topic", source="tree"),
            ContextEntry("회사/stacks/python", "topic", source="tree"),
            ContextEntry("업무 운영", "area", source="wiki"),
            ContextEntry("메모", "topic", source="wiki"),
        ]
    )
    area = snapshot.area_of("회사/infra/network/router")
    assert area is not None and area.name == "회사/infra/network"
    assert snapshot.area_of("회사/stacks/python") is None
    legacy = snapshot.area_of("업무 운영")
    assert legacy is not None and legacy.name == "업무 운영"
    assert snapshot.area_of("메모") is None
    assert snapshot.area_of("없음") is None
    assert snapshot.has_tree
