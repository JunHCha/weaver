from datetime import UTC, datetime

import pytest

from weaver.context import ContextDictionary, Page
from weaver.fabric.v2.store import FabricStore, ThreadQuery
from weaver.schema.v2 import ThreadDraft
from weaver.wiki.compiler import Compiler, EditResult, Summary
from weaver.wiki.store import WikiStore

NOW = datetime(2025, 1, 1, tzinfo=UTC)
SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"


class FakeLLM:
    def generate_text(self, task, system, user):
        return "본문 " + user

    def generate_json(self, task, system, user, schema):
        return EditResult(changes=[])


def draft():
    return ThreadDraft(
        thread_id="one",
        kind="기록",
        content="내용",
        occurred_at=NOW,
        recorded_at=NOW,
        tags=["홍길동"],
        field="status",
        value="진행",
        source_id=SOURCE,
        actor="ai_approved",
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
    )


def test_compile_and_bookmark_failure(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_from_pages([Page("홍길동", "person")])
    fabric.append([draft()])
    compiler = Compiler(
        store,
        fabric,
        context,
        FakeLLM(),
        tmp_path / "compiler.db",
        lambda _: Summary("2025-01-01 홍길동 call", "요약"),
    )
    compiler.run()
    props, body, _ = store.read("pages/홍길동.md")
    assert props["status"] == "진행"
    assert props["kind"] == "person"
    assert "본문" in body and "summaries/" in body
    assert compiler.bookmark() == 1
    assert len(fabric.entries(ThreadQuery())) == 1
    summary_path = next((store.root / "summaries").glob("*.md"))
    original = summary_path.read_text()
    summary_path.write_text("변경")
    compiler.run()
    assert summary_path.read_text() == original

    fabric.append([draft().model_copy(update={"thread_id": "two"})])

    class Failure(FakeLLM):
        def generate_text(self, task, system, user):
            raise RuntimeError("failure")

    compiler.llm = Failure()
    with pytest.raises(RuntimeError):
        compiler.run()
    assert compiler.bookmark() == 1


def test_human_edit_retry_and_fields(tmp_path):
    from weaver.wiki.compiler import EditChange
    from weaver.wiki.store import HumanEdit

    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    fabric.append([draft()])

    class Edits(FakeLLM):
        def generate_json(self, task, system, user, schema):
            from weaver.schema.v2 import TagCorrection

            return EditResult(
                changes=[
                    EditChange(content="새 사실", tags=["홍길동"]),
                    EditChange(
                        content="태그 정정",
                        tags=[],
                        correction=TagCorrection(
                            target="one", remove=["홍길동"], add=["김철수"]
                        ),
                    ),
                ]
            )

    compiler = Compiler(
        store,
        fabric,
        context,
        Edits(),
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    edit = HumanEdit(
        "pages/홍길동.md",
        "old",
        "new",
        "diff",
        {"status": "진행", "assignees": ["홍길동"]},
        {
            "status": "완료",
            "deadline": "2025-01-02",
            "schedule": "오후",
            "assignees": ["김철수"],
        },
    )
    store.acknowledge = lambda edit: None
    compiler._edit(edit)
    compiler._edit(edit)
    threads = fabric.entries(ThreadQuery())
    human = [t for t in threads if t.actor == "human"]
    assert len(human) == 6
    assert any(t.field == "status" and t.value == "완료" for t in human)
    assert {t.field for t in human if t.field} == {
        "status",
        "deadline",
        "schedule",
        "assignees",
    }
    assert next(t.value for t in human if t.field == "assignees") == {
        "add": ["김철수"],
        "remove": ["홍길동"],
    }
    assert any(t.correction is not None for t in human)
    assert next(t for t in threads if t.thread_id == "one").tags == ["김철수"]


def test_unsettled_human_edit_is_never_overwritten(tmp_path):
    import os
    import time

    from weaver.wiki.store import HumanEditPending

    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", settle_seconds=1800)
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_from_pages([Page("홍길동", "person")])
    fabric.append([draft()])
    compiler = Compiler(
        store,
        fabric,
        context,
        FakeLLM(),
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    compiler.run()
    path = store.root / "pages/홍길동.md"
    path.write_text(path.read_text() + "\n사람의 변경\n")
    old = time.time() - 3600
    os.utime(path, (old, old))
    fabric.append([draft().model_copy(update={"thread_id": "two"})])
    with pytest.raises(HumanEditPending):
        compiler.run()
    assert "사람의 변경" in path.read_text()
    assert compiler.bookmark() == 1


def test_settled_human_edit_is_used_without_feedback_loop(tmp_path):

    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_from_pages([Page("홍길동", "person")])
    fabric.append([draft()])
    compiler = Compiler(
        store,
        fabric,
        context,
        FakeLLM(),
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    compiler.run()
    path = store.root / "pages/홍길동.md"
    path.write_text(path.read_text() + "\n사람의 변경\n")
    fabric.append([draft().model_copy(update={"thread_id": "two"})])
    from weaver.wiki.store import HumanEditPending

    with pytest.raises(HumanEditPending):
        compiler.run()
    compiler.run()
    assert "사람의 변경" in path.read_text()
    assert len(fabric.entries(ThreadQuery())) == 2
    assert store.scan(datetime.now(UTC).timestamp()) == []


def test_frontmatter_follows_projected_fields_and_context(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_from_pages([Page("홍길동", "person", ("별명",))])
    changes = [
        ("deadline", "2025-01-02"),
        ("schedule", "오후"),
        ("assignees", {"add": ["김철수"], "remove": []}),
        ("status", "완료"),
    ]
    fabric.append(
        [
            draft().model_copy(
                update={"thread_id": str(index), "field": field, "value": value}
            )
            for index, (field, value) in enumerate(changes)
        ]
    )
    compiler = Compiler(
        store,
        fabric,
        context,
        FakeLLM(),
        tmp_path / "compiler.db",
        lambda _: Summary("2025-01-01 홍길동 call", "요약"),
    )
    compiler.run()
    props, _, _ = store.read("pages/홍길동.md")
    assert {
        key: props[key]
        for key in ("deadline", "schedule", "assignees", "status", "kind", "aliases")
    } == {
        "deadline": "2025-01-02",
        "schedule": "오후",
        "assignees": ["김철수"],
        "status": "완료",
        "kind": "person",
        "aliases": ["별명"],
    }
