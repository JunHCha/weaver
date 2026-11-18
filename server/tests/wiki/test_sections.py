"""Issue #166: isolated synthetic corpora, real private Git, and fake LLMs only."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.schema import ThreadDraft
from weaver.wiki.batch import BatchCompiler, WikiValidationError
from weaver.wiki.compiler import Summary
from weaver.wiki.document import HUMAN_KEYS, MEMO, NOTICE, sections
from weaver.wiki.migration import WikiMigration
from weaver.wiki.selection import EvidenceIndex
from weaver.wiki.store import WikiStore, _render

SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
PATH = "pages/운영.md"


def draft(key, tag, **updates):
    return ThreadDraft(
        thread_id=key,
        kind="기록",
        content=f"합성 사실 {key}",
        occurred_at=datetime(2026, 11, 18, tzinfo=UTC),
        recorded_at=datetime(2026, 11, 18, tzinfo=UTC),
        tags=[tag],
        source_id=SOURCE,
        actor="ai_approved",
        field=None,
        value=None,
        direction=None,
        resolves=None,
        retracts=None,
        correction=None,
        **updates,
    )


class Model:
    def __init__(self):
        self.calls = []
        self.verdict = "expression"
        self.drop_human = False
        self.extra_section = False

    def generate_json(self, task, system, user, schema):
        payload = json.loads(user)
        self.calls.append((task, payload))
        if task == "wiki.human_edit":
            return schema.model_validate(
                {
                    "hunks": [
                        {
                            "id": h["id"],
                            "classification": self.verdict,
                            "changes": [
                                {
                                    "content": "사람이 확인한 사실",
                                    "tags": ["네트워크", "업무 운영"],
                                }
                            ]
                            if self.verdict == "fact"
                            else [],
                            "section_threads": {"## 새 구조": ["one"]}
                            if self.verdict == "structure"
                            else {},
                        }
                        for h in payload["hunks"]
                    ]
                }
            )
        ids = [t["thread_id"] for t in payload["threads"]]
        scope = payload["section_scope"].get(PATH)
        if scope is None:
            body = "# 운영\n\n## 네트워크\n초기 상태\n\n## 도구\n사람이 고른 문장을 그대로 둡니다.\n"
            mapping = {"__intro__": [], "## 네트워크": ["one"], "## 도구": ["two"]}
        else:
            body = "# 운영\n\n"
            for key in scope:
                if key == "__intro__":
                    continue
                body += key + "\n새 상태\n\n"
            if not self.drop_human:
                for page in payload["existing_pages"]:
                    for span in page["protected_human_text"]:
                        if span["section"] in scope:
                            body += span["text"]
            mapping = {key: ids for key in scope}
        if self.extra_section:
            body += "\n## 도구\n모델이 잘못 다시 쓴 내용\n"
        return schema.model_validate(
            {
                "pages": [
                    {
                        "title": "운영",
                        "role": "operation",
                        "summary": "운영 현황",
                        "thread_ids": ids,
                        "state_tag": None,
                        "aliases": [],
                        "body": body,
                        "section_threads": mapping,
                    }
                ],
                "excluded_threads": [],
            }
        )

    def generate_text(self, *args):
        raise AssertionError("structured calls only")


def setup(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    fabric.append([draft("one", "네트워크"), draft("two", "도구")])
    context = ContextDictionary(tmp_path / "context")
    context.add_entry("업무 운영", "area")
    context.add_entry("네트워크", "topic")
    model = Model()
    compiler = BatchCompiler(
        store,
        fabric,
        context,
        model,
        tmp_path / "state/wiki-compiler.sqlite3",
        lambda _: Summary("11/18 합성 통화", "합성 요약"),
    )
    compiler.run()
    return store, fabric, model, compiler


def settle_edit(store, compiler, transform):
    page = store.root / PATH
    page.write_text(transform(page.read_text()))
    store.scan(store.clock())
    edits = store.scan(store.clock() + 1)
    assert len(edits) == 1
    compiler._edit(edits[0])
    return edits[0]


def records(compiler, rev):
    with sqlite3.connect(compiler.state_path) as db:
        return db.execute(
            "SELECT classification FROM edit_hunks WHERE rev=? ORDER BY hunk", (rev,)
        ).fetchall()


def test_partial_compile_changes_only_selected_section_and_preserves_memo(tmp_path):
    store, fabric, model, compiler = setup(tmp_path)
    settle_edit(
        store, compiler, lambda text: text + "절대로 모델에 보내지 않는 내 메모\n"
    )
    before = {s.key: s.text for s in sections(store.read(PATH)[1])}
    model.calls.clear()
    fabric.append([draft("three", "네트워크")])
    compiler.run()
    after = {s.key: s.text for s in sections(store.read(PATH)[1])}
    assert after["## 네트워크"] != before["## 네트워크"]
    assert after["## 도구"] == before["## 도구"]
    assert after["__intro__"] == before["__intro__"]
    assert after[MEMO] == before[MEMO]
    payload = model.calls[0][1]
    assert payload["section_scope"] == {PATH: ["## 네트워크"]}
    assert {t["thread_id"] for t in payload["threads"]} == {"one", "three"}
    assert "내 메모" not in json.dumps(payload, ensure_ascii=False)
    assert store.metadata(PATH)["sections"]["## 도구"]["thread_ids"] == ["two"]


def test_human_wording_in_selected_section_is_protected_and_bad_output_rejected(
    tmp_path,
):
    store, fabric, model, compiler = setup(tmp_path)
    settle_edit(
        store,
        compiler,
        lambda text: text.replace("초기 상태", "사람이 신중하게 쓴 문장"),
    )
    fabric.append([draft("three", "네트워크")])
    model.drop_human = True
    before = (store.root / PATH).read_bytes()
    with pytest.raises(WikiValidationError, match="human wording"):
        compiler.run()
    assert (store.root / PATH).read_bytes() == before
    model.drop_human = False
    # Change snapshot to retry an intentionally rejected result.
    fabric.append([draft("four", "네트워크")])
    compiler.run()
    assert "사람이 신중하게 쓴 문장" in store.read(PATH)[1]


def test_unselected_model_section_rejected_before_any_write(tmp_path):
    store, fabric, model, compiler = setup(tmp_path)
    before = (store.root / PATH).read_bytes()
    fabric.append([draft("three", "네트워크")])
    model.extra_section = True
    with pytest.raises(WikiValidationError, match="selected sections"):
        compiler.run()
    assert (store.root / PATH).read_bytes() == before
    assert compiler.bookmark() == 2


def test_bounded_input_token_estimate_is_smaller_than_full_synthetic_corpus(tmp_path):
    store, fabric, model, compiler = setup(tmp_path)
    unrelated = [
        draft(f"other-{i}", f"다른 업무 {i}").model_copy(
            update={"content": "unrelated synthetic evidence " * 100}
        )
        for i in range(200)
    ]
    fabric.append(unrelated)
    with sqlite3.connect(compiler.state_path) as db:
        db.execute("UPDATE bookmark SET seq=202")
    fabric.append([draft("three", "네트워크")])
    compiler.run()
    payload = json.dumps(model.calls[-1][1], ensure_ascii=False)
    full = json.dumps(
        [t.model_dump(mode="json") for t in fabric.entries(ThreadQuery())],
        ensure_ascii=False,
    )
    # Same deterministic UTF-8/4 estimator for both sides, not provider billing.
    partial_tokens = (len(payload.encode()) + 3) // 4
    full_tokens = (len(full.encode()) + 3) // 4
    print(
        f"INPUT_TOKEN_ESTIMATE partial={partial_tokens} full={full_tokens} reduction={1 - partial_tokens / full_tokens:.2%}"
    )
    assert partial_tokens < full_tokens / 10
    assert all("other-" not in t["thread_id"] for t in model.calls[-1][1]["threads"])


@pytest.mark.parametrize(
    "kind, transform, calls",
    [
        ("memo", lambda text: text + "개인 생각\n", 0),
        ("expression", lambda text: text.replace("초기 상태", "**초기   상태**"), 0),
        ("expression", lambda text: text.replace("초기 상태", "초기\n상태"), 0),
        (
            "metadata",
            lambda text: text.replace("weaver: ", 'state_tag: "위조"\nweaver: '),
            0,
        ),
        ("content", lambda text: text.replace("초기 상태", "새로운 사실"), 1),
    ],
)
def test_code_first_hunk_call_counts(tmp_path, kind, transform, calls):
    store, fabric, model, compiler = setup(tmp_path)
    model.calls.clear()
    edit = settle_edit(store, compiler, transform)
    assert len(model.calls) == calls
    expected = "expression" if kind == "content" else kind
    assert (expected,) in records(compiler, edit.new_rev)
    assert len(fabric.entries(ThreadQuery())) == 2
    if kind == "metadata":
        assert "위조" not in (store.root / PATH).read_text()
        assert store.metadata(PATH)["state_tag"] is None


def test_frontmatter_value_change_creates_thread_without_llm(tmp_path):
    store, fabric, model, compiler = setup(tmp_path)
    store.write_metadata(PATH, {"state_tag": "네트워크"})
    model.calls.clear()
    edit = settle_edit(
        store,
        compiler,
        lambda text: text.replace("---\n", '---\ndeadline: "2026-12-01"\n', 1),
    )
    assert model.calls == []
    added = [t for t in fabric.entries(ThreadQuery()) if t.actor == "human"]
    assert len(added) == 1 and added[0].field == "deadline"
    assert ("frontmatter",) in records(compiler, edit.new_rev)


def test_mixed_hunks_use_one_call_and_record_each_verdict_once(tmp_path):
    store, fabric, model, compiler = setup(tmp_path)
    model.verdict = "fact"
    model.calls.clear()
    edit = settle_edit(
        store,
        compiler,
        lambda text: (
            text.replace("초기 상태", "네트워크 변경").replace(
                "사람이 고른 문장을 그대로 둡니다.", "도구 변경"
            )
            + "메모 추가\n"
        ),
    )
    assert len(model.calls) == 1
    assert len(model.calls[0][1]["hunks"]) == 2
    assert records(compiler, edit.new_rev) == [("fact",), ("fact",), ("memo",)]
    assert len([t for t in fabric.entries(ThreadQuery()) if t.actor == "human"]) == 2
    compiler._edit(edit)
    assert len(model.calls) == 1
    assert len(records(compiler, edit.new_rev)) == 3


def test_structure_verdict_updates_correspondence_without_thread(tmp_path):
    store, fabric, model, compiler = setup(tmp_path)
    model.verdict = "structure"
    model.calls.clear()
    edit = settle_edit(
        store, compiler, lambda text: text.replace("## 네트워크", "## 새 구조")
    )
    assert len(model.calls) == 1
    mapping = store.metadata(PATH)["sections"]
    assert mapping["## 새 구조"]["thread_ids"] == ["one"]
    assert "## 네트워크" not in mapping
    assert mapping["## 도구"]["thread_ids"] == ["two"]
    assert len(fabric.entries(ThreadQuery())) == 2
    assert all(row == ("structure",) for row in records(compiler, edit.new_rev))


def test_machine_region_reverted_and_readonly_restored_without_llm(tmp_path):
    store, _, model, compiler = setup(tmp_path)
    original = store.read(PATH)[1]
    model.calls.clear()
    settle_edit(
        store,
        compiler,
        lambda text: text.replace("## 근거\n", "## 근거\n- 위조 근거\n"),
    )
    assert model.calls == []
    assert store.read(PATH)[1] == original
    store.write("열린 요청.md", {}, "# 열린 요청\n", None, readonly=True)
    page = store.root / "열린 요청.md"
    assert NOTICE in page.read_text()
    page.write_text("사람 수정")
    compiler.run()
    assert "사람 수정" not in page.read_text()
    assert model.calls == []


def test_private_metadata_and_publication_keep_pipeline_page_contract(tmp_path):
    store, _, _, _ = setup(tmp_path)
    props, body, digest = store.read(PATH)
    assert set(props).issubset(HUMAN_KEYS | {"weaver"})
    assert "null" not in (store.root / PATH).read_text()
    assert "thread_ids" not in (store.root / PATH).read_text()
    assert "|11/18 합성 통화]]" in body
    assert not (store.root / "meta").exists()
    meta = store.metadata(PATH)
    assert meta["id"] == props["weaver"] and meta["hash"] == digest
    assert meta["sections"]["## 네트워크"]["thread_ids"] == ["one"]
    published = {path: data for path, data, _ in store.published_pages()}
    assert published[PATH] == (store.root / PATH).read_bytes()
    assert not any(path.startswith("meta/") for path in published)
    from weaver.schema import PipelinePage

    page = PipelinePage(path=PATH, updated_at=datetime.now(UTC), sha256=digest)
    assert page.path == PATH and page.sha256 == digest


def test_sql_counterpart_and_fts_are_supplementary_selectors(tmp_path):
    one = draft("one", "네트워크", counterpart="합성 사람")
    two = draft("two", "도구").model_copy(update={"content": "unique_search_word"})
    fabric = FabricStore(tmp_path / "fabric.db")
    threads = fabric.append([one, two])
    index = EvidenceIndex(tmp_path / "index.db", threads)
    assert index.related([], ["합성 사람"]) == {"one"}
    assert index.related([], [], "unique_search_word") == {"two"}
    assert index.related(["네트워크"], [], "unique_search_word") == {"one"}


def test_migration_preview_backup_apply_and_idempotency(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    legacy = _render(
        {
            "state_tag": "네트워크",
            "page_role": "operation",
            "thread_ids": ["one"],
            "deadline": None,
            "status": "진행",
        },
        "# 운영\n\n## 네트워크\n원래 문장\n\n## 내 메모\n사람 메모\n",
    )
    file = store._path(PATH)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(legacy)
    migration = WikiMigration(store, [])
    head = store._git("rev-parse", "HEAD")
    plan = migration.preview()
    assert file.read_bytes() == legacy and store._git("rev-parse", "HEAD") == head
    backup = tmp_path / "backup"
    with pytest.raises(ValueError, match="confirm"):
        migration.apply(plan, backup, confirmed=False)
    assert not backup.exists()
    assert migration.apply(plan, backup, confirmed=True) == 1
    assert (backup / "files" / PATH).read_bytes() == legacy
    assert (backup / "wiki.git/HEAD").exists() and (backup / "wiki.sqlite3").exists()
    assert "사람 메모" in file.read_text()
    assert store.metadata(PATH)["sections"]["## 네트워크"]["thread_ids"] == ["one"]
    assert migration.preview()["changes"] == []
    assert (
        migration.apply(migration.preview(), tmp_path / "unused-backup", confirmed=True)
        == 0
    )
    assert not (tmp_path / "unused-backup").exists()


def test_stale_migration_plan_fails_before_backup_or_write(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    file = store._path(PATH)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("# 운영\n이전\n")
    migration = WikiMigration(store, [])
    plan = migration.preview()
    file.write_text("# 운영\n사람이 방금 수정\n")
    with pytest.raises(ValueError, match="stale"):
        migration.apply(plan, tmp_path / "backup", confirmed=True)
    assert not (tmp_path / "backup").exists()
    assert file.read_text().endswith("사람이 방금 수정\n")


def test_memo_subheadings_and_fenced_headings_never_enter_the_prompt(tmp_path):
    store, fabric, model, compiler = setup(tmp_path)
    settle_edit(
        store,
        compiler,
        lambda text: text + "\n## 개인 제목\n```md\n## 가짜 제목\n```\n비공개 생각\n",
    )
    model.calls.clear()
    fabric.append([draft("three", "네트워크")])
    compiler.run()
    assert "비공개 생각" in store.read(PATH)[1]
    assert "비공개 생각" not in json.dumps(model.calls, ensure_ascii=False)
    parsed = sections("# 제목\n```md\n## 예시\n```\n## 실제\n본문\n")
    assert [s.key for s in parsed] == ["__intro__", "## 실제"]


def test_sql_selector_does_not_miss_new_evidence_after_64_old_matches(tmp_path):
    from weaver.wiki.selection import affected

    fabric = FabricStore(tmp_path / "fabric.db")
    threads = fabric.append(
        [draft(f"old-{i}", "동일 업무") for i in range(100)]
        + [draft("zzz-new", "동일 업무")]
    )
    index = EvidenceIndex(tmp_path / "index.db", threads)
    meta = {"sections": {"## 현재": {"tags": ["동일 업무"], "thread_ids": ["old-0"]}}}
    assert affected(meta, [threads[-1]], index) == {"## 현재"}


def test_refresh_index_keeps_unaffected_pages_after_partial_compile(tmp_path):
    store, fabric, _, compiler = setup(tmp_path)
    store.write(
        "pages/다른 문서.md",
        {"page_role": "reference", "summary": "기존 기술 문서"},
        "# 다른 문서\n",
        None,
    )
    fabric.append([draft("three", "네트워크")])
    compiler.run()
    compiler.refresh_index()
    body = store.read("index.md")[1]
    assert "[[pages/다른 문서]]" in body and "[[pages/운영]]" in body


def test_initial_compilation_uses_a_bounded_sequence_window(tmp_path):
    from weaver.wiki.selection import MAX_NEW_THREADS

    class InitialModel(Model):
        def generate_json(self, task, system, user, schema):
            payload = json.loads(user)
            self.calls.append((task, payload))
            return schema.model_validate(
                {
                    "pages": [
                        {
                            "title": "초기 업무",
                            "role": "operation",
                            "summary": "합성 문서",
                            "thread_ids": [t["thread_id"] for t in payload["threads"]],
                            "state_tag": None,
                            "aliases": [],
                            "body": "# 초기 업무\n초기 상태\n",
                        }
                    ],
                    "excluded_threads": [],
                }
            )

    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    fabric.append([draft(f"thread-{i}", "합성 업무") for i in range(100)])
    model = InitialModel()
    compiler = BatchCompiler(
        store,
        fabric,
        ContextDictionary(tmp_path / "context"),
        model,
        tmp_path / "compiler.db",
        lambda _: Summary("합성 통화", "합성 요약"),
    )
    compiler.run()
    assert len(model.calls[0][1]["threads"]) == MAX_NEW_THREADS
    assert compiler.bookmark() == MAX_NEW_THREADS


def test_migration_with_legacy_links_is_idempotent(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    file = store._path(PATH)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(
        _render(
            {"thread_ids": ["one"], "state_tag": "네트워크"},
            "# 운영\n\n## 현황\n[[summaries/합성 통화 src_01ARZ3NDEKTSV4RRFFQ69G5FAV]]\n",
        )
    )
    migration = WikiMigration(store, [])
    migration.apply(migration.preview(), tmp_path / "backup", confirmed=True)
    assert migration.preview()["changes"] == []


def test_missing_hunk_verdict_is_retried_without_fact_or_acknowledgement(tmp_path):
    store, _, model, compiler = setup(tmp_path)
    from weaver.wiki.compiler import EditResult

    model.generate_json = lambda *args: EditResult(changes=[])
    edit = settle_edit(
        store, compiler, lambda text: text.replace("초기 상태", "판정 필요")
    )
    assert store.scan(store.clock() + 2)[0].new_rev == edit.new_rev
    assert records(compiler, edit.new_rev) == []


def test_migration_preserves_precise_maps_of_already_converted_pages(tmp_path):
    store, fabric, _, _ = setup(tmp_path)
    before = store.metadata(PATH)
    migration = WikiMigration(store, fabric.entries(ThreadQuery()))
    assert migration.preview()["changes"] == []
    assert store.metadata(PATH) == before


def test_section_assignments_survive_strict_provider_wire_schema():
    from jsonschema import Draft202012Validator

    from weaver.llm.schemas import openai_schema
    from weaver.wiki.batch import WikiPage
    from weaver.wiki.compiler import HunkVerdict

    page = WikiPage(
        title="운영",
        role="operation",
        summary="합성 문서",
        thread_ids=["one"],
        state_tag=None,
        aliases=[],
        body="# 운영\n\n## 현재\n본문\n",
        section_threads=[{"section": "## 현재", "thread_ids": ["one"]}],
    )
    verdict = HunkVerdict(
        id=0,
        classification="structure",
        section_threads=[{"section": "## 현재", "thread_ids": ["one"]}],
    )
    for model, value in ((WikiPage, page), (HunkVerdict, verdict)):
        wire = openai_schema(model)
        Draft202012Validator(wire).validate(value.model_dump())
        assert wire["$defs"]["SectionAssignments"]["type"] == "array"
