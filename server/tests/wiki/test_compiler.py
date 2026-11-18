import json
import os
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from server.tests.fake_responses import fixture_response

from weaver.context import ContextDictionary, Page
from weaver.fabric.store import FabricStore, ThreadQuery
from weaver.schema import ThreadDraft
from weaver.wiki.compiler import Compiler, EditResult, Summary
from weaver.wiki.store import HumanEdit, WikiStore

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


@pytest.mark.parametrize("legacy_confirmed", [False, True])
def test_non_markdown_attachments_never_call_llm_or_create_threads(
    tmp_path, legacy_confirmed
):
    now = [10000.0]
    store = WikiStore(tmp_path / "vault", tmp_path / "wiki", clock=lambda: now[0])
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")

    class NoCalls(FakeLLM):
        def generate_text(self, task, system, user):
            pytest.fail(f"unexpected LLM call: {task}")

        def generate_json(self, task, system, user, schema):
            pytest.fail(f"unexpected LLM call: {task}")

    compiler = Compiler(
        store,
        fabric,
        context,
        NoCalls(),
        tmp_path / "compiler.db",
        lambda _: Summary("합성 요약", "본문"),
    )
    for suffix in ("png", "apk", "txt", "json", "MD"):
        path = store.root / f"attachment.{suffix}"
        path.write_bytes(b"\xff" if suffix in ("png", "apk") else b"synthetic text")
        os.utime(path, (now[0], now[0]))
    if legacy_confirmed:
        with store._db() as db:
            for suffix in ("txt", "json", "MD"):
                edit = HumanEdit(f"attachment.{suffix}", "base", suffix, "diff", {}, {})
                payload = json.dumps(asdict(edit))
                db.execute("INSERT INTO pending VALUES (?, ?)", (suffix, payload))
                db.execute("INSERT INTO human_edits VALUES (?, ?)", (suffix, payload))
        store = WikiStore(tmp_path / "vault", tmp_path / "wiki", clock=lambda: now[0])
        compiler.store = store
    compiler.run()
    now[0] += 1801
    compiler.run()
    assert fabric.entries(ThreadQuery()) == []
    assert store.edit_states() == []
    with store._db() as db:
        assert db.execute("SELECT path FROM pages").fetchall() == []


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

    context.sync_from_pages([Page("홍길동", "person"), Page("김철수", "person")])

    class Edits(FakeLLM):
        def generate_json(self, task, system, user, schema):
            from weaver.schema import TagCorrection

            return fixture_response(
                EditResult,
                dict(
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
                ),
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
        {"state_tag": "홍길동", "status": "진행", "assignees": ["홍길동"]},
        {
            "state_tag": "홍길동",
            "status": "완료",
            "deadline": "2025-01-02",
            "schedule": {
                "start": "2025-01-02T15:00:00Z",
                "end": None,
                "timezone": "UTC",
            },
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
        ("schedule", {"start": "2025-01-02T15:00:00Z", "end": None, "timezone": "UTC"}),
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
        "schedule": {"start": "2025-01-02T15:00:00Z", "end": None, "timezone": "UTC"},
        "assignees": ["김철수"],
        "status": "완료",
        "kind": "person",
        "aliases": ["별명"],
    }


def test_edit_uses_shared_rules_dictionary_validation_and_durable_proposals(
    tmp_path, caplog
):
    import json

    from weaver.loom.tagging import TAG_CONTEXT_TOKEN_BUDGET, NewEntry
    from weaver.wiki.compiler import EditChange
    from weaver.wiki.store import HumanEdit

    vault = tmp_path / "vault"
    vault.mkdir()
    store = WikiStore(vault, tmp_path / "wiki", settle_seconds=0)
    store.acknowledge = lambda edit: None
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.add_entry("홍길동", "person", ("가명",))

    class Llm(FakeLLM):
        calls = 0

        def generate_json(self, task, system, user, schema):
            self.calls += 1
            for rule in (
                "녹취 해석(오인식·용어 뜻)",
                "직함·소속",
                "new_entries",
                "오인식·오타·띄어쓰기",
                "frontmatter 필드 변경은 코드",
            ):
                assert rule in system
            payload = json.loads(user)
            assert (
                len(
                    json.dumps(
                        {key: payload[key] for key in ("dictionary", "existing_tags")},
                        ensure_ascii=False,
                    ).encode()
                )
                <= TAG_CONTEXT_TOKEN_BUDGET
            )
            return fixture_response(
                EditResult,
                dict(
                    new_entries=[
                        NewEntry(name="김철수", kind="person", aliases=["김 철수"]),
                        NewEntry(name="지원팀 김영희 팀장님", kind="person"),
                    ],
                    changes=[
                        EditChange(
                            content="홍길동과 김철수가 자료를 전달했다.",
                            tags=[
                                "가명",
                                "김 철수",
                                "비공개미등록",
                                "지원팀 김영희 팀장님",
                            ],
                        )
                    ],
                ),
            )

    llm = Llm()
    compiler = Compiler(
        store,
        fabric,
        context,
        llm,
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    edit = HumanEdit("pages/자료.md", "old", "new", "합성 변경", {}, {})
    original = fabric.append

    def fail(drafts):
        assert context.resolve("김철수") is not None
        raise RuntimeError("synthetic interruption")

    fabric.append = fail
    with pytest.raises(RuntimeError):
        compiler._edit(edit)
    fabric.append = original
    compiler._edit(edit)
    assert llm.calls == 1
    assert fabric.entries(ThreadQuery())[0].tags == ["홍길동", "김철수", "업무 운영"]
    assert not context.resolve("김철수").human_edited
    assert context.resolve("김철수").aliases == ()
    assert "dropped unresolved approval tag" in caplog.text
    assert "비공개미등록" not in caplog.text


def test_field_sentences_state_tag_area_and_no_stem_fallback(tmp_path, caplog):
    from weaver.wiki.compiler import _field_text
    from weaver.wiki.store import HumanEdit

    assert (
        _field_text("assignees", [], {"add": ["김철수"], "remove": ["김영희"]})
        == "담당자에 김철수 추가, 담당자에서 김영희 제외"
    )
    assert (
        _field_text("deadline", "2032-11-20", "2032-11-25")
        == "마감을 11/20에서 11/25로 변경"
    )
    assert (
        _field_text(
            "schedule",
            None,
            {"start": "2032-11-18T13:30:00+09:00", "end": "2032-11-18T14:00:00+09:00"},
        )
        == "일정을 11/18 13:30–14:00으로 변경"
    )
    assert _field_text("status", None, "보류") == "상태를 보류로 변경"
    store = WikiStore(tmp_path / "vault", tmp_path / "wiki", settle_seconds=0)
    store.acknowledge = lambda _: None
    context = ContextDictionary(tmp_path / "context")
    context.add_entry("자료", "topic", ("자료별칭",))
    context.add_entry("자료 관리", "area")
    fabric = FabricStore(tmp_path / "fabric.db")
    fabric.append(
        [
            draft().model_copy(
                update={"tags": ["자료", "자료 관리"], "counterpart": "김철수"}
            )
        ]
    )
    compiler = Compiler(
        store,
        fabric,
        context,
        FakeLLM(),
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    compiler._edit(
        HumanEdit(
            "pages/절대태그아님.md",
            "old",
            "new",
            "",
            {"state_tag": "자료별칭", "status": "진행"},
            {"state_tag": "자료별칭", "status": "보류"},
        )
    )
    generated = fabric.entries(ThreadQuery())[-1]
    assert generated.thread_id.startswith("wiki-field:")
    assert generated.tags == ["자료", "자료 관리"]
    assert generated.content == "상태를 보류로 변경"
    assert generated.counterpart is None
    compiler._edit(
        HumanEdit("pages/절대태그아님.md", "new", "missing", "", {}, {"status": "완료"})
    )
    assert len(fabric.entries(ThreadQuery())) == 2
    assert "skipped wiki fields without a resolved state tag" in caplog.text
    assert "절대태그아님" not in caplog.text


def test_wiki_roles_sync_to_para_kinds(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "wiki", settle_seconds=0)
    context = ContextDictionary(tmp_path / "context")
    compiler = Compiler(
        store,
        FabricStore(tmp_path / "fabric.db"),
        context,
        FakeLLM(),
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    for role, _expected in (
        ("project", "project"),
        ("operation", "area"),
        ("reference", "topic"),
        ("person", "person"),
        ("organization", "organization"),
    ):
        store.write(
            f"pages/{role}.md", {"kind": "topic", "page_role": role}, "본문", None
        )
    compiler._sync_pages()
    assert {entry.name: entry.kind for entry in context.list_entries()} == {
        "project": "project",
        "operation": "area",
        "reference": "topic",
        "person": "person",
        "organization": "organization",
    }


@pytest.mark.parametrize(
    "status,expected",
    [
        ("보류", "보류로"),
        ("진행", "진행으로"),
        ("대기실", "대기실로"),
        ("완료", "완료로"),
        ("확인중", "확인중으로"),
    ],
)
def test_field_text_korean_direction_particle(status, expected):
    from weaver.wiki.compiler import _field_text

    assert _field_text("status", None, status) == f"상태를 {expected} 변경"


def test_wiki_prompt_is_specific_and_counterpart_is_a_tag(tmp_path):
    from weaver.wiki.compiler import EditChange
    from weaver.wiki.prompts import (
        HUMAN_EDIT_PROMPT,
        WIKI_EDIT_OUTPUT_RULES,
        WIKI_EDIT_RULES,
    )
    from weaver.wiki.store import HumanEdit

    assert "\n" in HUMAN_EDIT_PROMPT
    assert HUMAN_EDIT_PROMPT.startswith(WIKI_EDIT_RULES + "\n")
    assert HUMAN_EDIT_PROMPT.endswith("\n" + WIKI_EDIT_OUTPUT_RULES)
    for forbidden in ("contact_name", "기록 주인", "검토 답", "corrections", "#149"):
        assert forbidden not in HUMAN_EDIT_PROMPT
    assert "태그 정정에는 영역이 필요 없다" in HUMAN_EDIT_PROMPT
    store = WikiStore(tmp_path / "vault", tmp_path / "wiki", settle_seconds=0)
    store.acknowledge = lambda _: None
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.add_entry("자료 관리", "area")
    context.add_entry("김철수", "person", ("김 박사",))

    class Party(FakeLLM):
        def generate_json(self, task, system, user, schema):
            return EditResult(
                changes=[
                    EditChange(
                        content="김철수가 자료를 전달했다.",
                        tags=["자료 관리"],
                        counterpart="김 박사",
                    )
                ]
            )

    compiler = Compiler(
        store,
        fabric,
        context,
        Party(),
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    compiler._edit(HumanEdit("pages/자료.md", "old", "new", "합성 변경", {}, {}))
    result = fabric.entries(ThreadQuery())[0]
    assert result.counterpart == "김철수"
    assert result.tags == ["자료 관리", "김철수"]


@pytest.mark.parametrize("failure", ["area", "schema", "json", "missing", "refusal"])
def test_wiki_contract_failure_is_bounded_durable_and_acknowledged_without_threads(
    tmp_path, caplog, failure
):
    from weaver.llm.errors import InvalidJson, MissingOutput, Refusal, SchemaMismatch
    from weaver.wiki.compiler import MAX_EDIT_CONTRACT_FAILURES, EditChange
    from weaver.wiki.store import HumanEdit

    store = WikiStore(tmp_path / "vault", tmp_path / "wiki", settle_seconds=0)
    acknowledged = []
    store.acknowledge = acknowledged.append
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")

    class Bad(FakeLLM):
        calls = 0

        def generate_json(self, task, system, user, schema):
            self.calls += 1
            if failure != "area":
                raise {
                    "schema": SchemaMismatch,
                    "json": InvalidJson,
                    "missing": MissingOutput,
                    "refusal": Refusal,
                }[failure]()
            return EditResult(
                changes=[EditChange(content="SYNTHETIC_PRIVATE_FACT", tags=[])]
            )

    llm = Bad()
    edit = HumanEdit(
        "pages/SYNTHETIC_PRIVATE_PAGE.md",
        "old",
        "new",
        "SYNTHETIC_PRIVATE_DIFF",
        {},
        {},
    )
    compiler = Compiler(
        store,
        fabric,
        context,
        llm,
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    for _ in range(MAX_EDIT_CONTRACT_FAILURES - 1):
        compiler._edit(edit)
        assert not acknowledged and not fabric.entries(ThreadQuery())
    # Retry counts survive a process restart.
    restarted = Compiler(
        store,
        fabric,
        context,
        llm,
        tmp_path / "compiler.db",
        lambda _: Summary("요약", "본문"),
    )
    restarted._edit(edit)
    assert acknowledged == [edit]
    assert llm.calls == MAX_EDIT_CONTRACT_FAILURES
    assert not fabric.entries(ThreadQuery())
    restarted._edit(edit)
    assert llm.calls == MAX_EDIT_CONTRACT_FAILURES
    assert "acknowledged without threads" in caplog.text
    assert "SYNTHETIC_PRIVATE" not in caplog.text
