"""PR #171 review reproductions exp1..exp6 plus approved operating decisions."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from server.tests.wiki import test_sections as T

from weaver.context import ContextDictionary
from weaver.fabric import FabricStore, ThreadQuery
from weaver.wiki.document import MEMO, expression, readable_links, sections
from weaver.wiki.measurement import measure
from weaver.wiki.migration import WikiMigration
from weaver.wiki.queries import write_pages
from weaver.wiki.selection import EvidenceIndex, affected, section_map
from weaver.wiki.store import WikiStore, _render


def legacy(store, body="# 운영\n\n## 현재\n원래 문장\n", props=None):
    props = props or {"state_tag": "네트워크", "thread_ids": ["one", "two"]}
    data = _render(props, body)
    file = store._path(T.PATH)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    rev = store._commit(T.PATH, data, "weaver", "legacy baseline")
    with store._db() as db:
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, 0, NULL, NULL, NULL)",
            (T.PATH, data, rev),
        )
    return file, data


@pytest.mark.parametrize(
    "state", ["hash", "candidate", "pending", "human_edits", "legacy_hash"]
)
def test_exp1_migration_never_absorbs_unprocessed_human_edits(tmp_path, state):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    file, baseline = legacy(store)
    if state == "hash":
        file.write_bytes(baseline.replace("원래".encode(), "사람".encode()))
    elif state == "candidate":
        with store._db() as db:
            db.execute("UPDATE pages SET candidate='observed'")
    elif state in ("pending", "human_edits"):
        with store._db() as db:
            db.execute(
                f"INSERT INTO {state} VALUES (?, ?)",
                ("human-rev", json.dumps({"path": T.PATH})),
            )
    else:
        bad = baseline.replace("원래".encode(), "수정".encode())
        file.write_bytes(bad)
        with store._db() as db:
            db.execute("UPDATE pages SET content=?", (bad,))
    before = file.read_bytes()
    migration = WikiMigration(store, [])
    plan = migration.preview()
    assert plan["changes"] == []
    assert plan["skipped"] == [{"path": T.PATH, "reason": "먼저 컴파일러로 처리"}]
    assert migration.apply(plan, tmp_path / "backup", confirmed=True) == 0
    assert file.read_bytes() == before
    assert not (tmp_path / "backup").exists()


def test_exp1_apply_rechecks_edits_after_preview(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    file, before = legacy(store)
    migration = WikiMigration(store, [])
    plan = migration.preview()
    file.write_bytes(before + "사람의 새 문장".encode())
    assert migration.apply(plan, tmp_path / "backup", confirmed=True) == 0
    assert migration.last_report["skipped"][0]["reason"] == "먼저 컴파일러로 처리"
    assert "사람의 새 문장" in file.read_text()


def test_exp2_page_move_uses_id_without_model_or_thread(tmp_path, monkeypatch):
    store, fabric, model, compiler = T.setup(tmp_path)
    old_meta = store.metadata(T.PATH)
    new = "pages/운영 개편.md"
    (store.root / T.PATH).rename(store.root / new)
    model.calls.clear()
    assert store.scan(store.clock()) == []
    assert store.scan(store.clock() + 1) == []
    assert model.calls == [] and len(fabric.entries(ThreadQuery())) == 2
    assert store.metadata(new)["id"] == old_meta["id"]
    assert store.metadata(new)["sections"] == old_meta["sections"]
    assert store.metadata(T.PATH) == {}
    monkeypatch.setattr(T, "PATH", new)
    fabric.append([T.draft("three", "네트워크")])
    compiler.run()
    assert "새 상태" in store.read(new)[1]
    assert not (store.root / "pages/운영.md").exists()


def test_exp2_duplicate_id_gets_new_identity_without_copying_metadata(tmp_path):
    store, fabric, model, _ = T.setup(tmp_path)
    new = "pages/운영 복사.md"
    (store.root / new).write_bytes((store.root / T.PATH).read_bytes())
    model.calls.clear()
    store.scan(store.clock())
    assert store.scan(store.clock() + 1) == []
    assert store.read(new)[0]["weaver"] != store.read(T.PATH)[0]["weaver"]
    assert store.metadata(new)["thread_ids"] == []
    assert all(
        data["thread_ids"] == [] for data in store.metadata(new)["sections"].values()
    )
    assert model.calls == [] and len(fabric.entries(ThreadQuery())) == 2


def test_exp3_shared_area_does_not_select_unrelated_sections(tmp_path):
    dictionary = ContextDictionary(tmp_path / "context")
    dictionary.add_entry("업무 운영", "area")
    fabric = FabricStore(tmp_path / "fabric.db")
    old = fabric.append([T.draft("one", "네트워크"), T.draft("two", "도구")])
    mapping = section_map(
        "# 운영\n\n## 네트워크\nx\n\n## 도구\ny\n",
        old,
        {"__intro__": [], "## 네트워크": ["one"], "## 도구": ["two"]},
    )
    new = fabric.append([T.draft("three", "도구")])
    index = EvidenceIndex(tmp_path / "index.db", old + new, dictionary.snapshot())
    assert affected({"sections": mapping}, new, index) == {"## 도구"}
    unrelated = fabric.append([T.draft("four", "백업")])
    index = EvidenceIndex(
        tmp_path / "index.db", old + new + unrelated, dictionary.snapshot()
    )
    assert affected({"sections": mapping}, unrelated, index) == set()


def test_exp4_identical_query_render_does_not_commit_or_touch_mtime(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    fabric, context = (
        FabricStore(tmp_path / "fabric.db"),
        ContextDictionary(tmp_path / "context"),
    )
    now = datetime(2026, 11, 19, tzinfo=UTC)
    write_pages(fabric, context, store, now=now)
    before = store._git("rev-parse", "HEAD")
    mtimes = {p.name: p.stat().st_mtime_ns for p in store.root.glob("*.md")}
    write_pages(fabric, context, store, now=now)
    assert store._git("rev-parse", "HEAD") == before
    assert {p.name: p.stat().st_mtime_ns for p in store.root.glob("*.md")} == mtimes


def test_exp5_partial_compile_keeps_evidence_before_memo_and_adds_new_source(tmp_path):
    store, fabric, _, compiler = T.setup(tmp_path)
    T.settle_edit(store, compiler, lambda text: text + "개인 메모\n")
    new_source = "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"
    fabric.append(
        [T.draft("three", "네트워크").model_copy(update={"source_id": new_source})]
    )
    compiler.run()
    body = store.read(T.PATH)[1]
    assert body.index("## 근거") < body.index(MEMO)
    assert T.SOURCE in body and new_source in body
    assert body.endswith("개인 메모\n")


def test_exp6_heading_rename_moves_mapping_in_code_and_next_compile_updates_it(
    tmp_path,
):
    store, fabric, model, compiler = T.setup(tmp_path)
    model.calls.clear()
    model.verdict = "expression"
    edit = T.settle_edit(
        store, compiler, lambda text: text.replace("## 네트워크", "## 네트워크 구성")
    )
    assert model.calls == []
    mapping = store.metadata(T.PATH)["sections"]
    assert "## 네트워크" not in mapping and mapping["## 네트워크 구성"][
        "thread_ids"
    ] == ["one"]
    assert ("structure",) in T.records(compiler, edit.new_rev)
    fabric.append([T.draft("three", "네트워크")])
    compiler.run()
    assert "새 상태" in store.read(T.PATH)[1]
    assert compiler.bookmark() == 3


def test_code_hunks_commit_before_a_failing_content_model(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)

    def failure(*args):
        raise RuntimeError("synthetic outage")

    model.generate_json = failure
    file = store.root / T.PATH
    file.write_text(
        file.read_text()
        .replace("---\n", '---\ndeadline: "2026-12-01"\n', 1)
        .replace("초기 상태", "새 업무 사실")
        + "메모\n"
    )
    store.scan(store.clock())
    edit = store.scan(store.clock() + 1)[0]
    with pytest.raises(RuntimeError):
        compiler._edit(edit)
    assert any(
        t.field == "deadline" and t.actor == "human"
        for t in fabric.entries(ThreadQuery())
    )
    assert ("memo",) in T.records(compiler, edit.new_rev)
    assert store.scan(store.clock() + 2)[0].new_rev == edit.new_rev


def test_unknown_field_scope_stays_pending(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    store.write_metadata(T.PATH, {"state_tag": None, "thread_ids": []})
    model.calls.clear()
    edit = T.settle_edit(
        store,
        compiler,
        lambda text: text.replace("---\n", '---\ndeadline: "2026-12-01"\n', 1),
    )
    assert store.scan(store.clock() + 2)[0].new_rev == edit.new_rev
    assert not [t for t in fabric.entries(ThreadQuery()) if t.actor == "human"]
    assert model.calls == []


def test_migration_reports_parse_errors_and_unmapped_sections(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    legacy(
        store,
        props={
            "thread_ids": ["one"],
            "tags": ["읽기"],
            "cssclasses": ["wide"],
            "date": "2026-11-19",
            "custom": "machine",
        },
    )
    (store.root / "pages/bad.md").write_text("---\ninvalid yaml\n---\n")
    plan = WikiMigration(store, []).preview()
    assert plan["errors"] == [{"path": "pages/bad.md", "reason": "변환 불가"}]
    item = plan["changes"][0]
    assert "custom" in item["moved_keys"] and "tags" not in item["moved_keys"]
    assert all(
        data["thread_ids"] == [] and data["status"] == "미분류"
        for data in item["metadata"]["sections"].values()
    )


def test_formatting_rules_and_fenced_links():
    assert expression("~~취소~~") != expression("취소")
    assert expression("> 인용") != expression("인용")
    assert expression("1 2") != expression("12")
    assert expression("server one") != expression("serverone")
    assert expression("한 글") == expression("한글")
    body = "```md\n[[summaries/예시 src_ABC]]\n```\n[[summaries/실제 src_ABC]]\n"
    result = readable_links(body)
    assert "[[summaries/예시 src_ABC]]" in result
    assert "[[summaries/실제 src_ABC|실제]]" in result


def test_measurement_is_read_only_with_realistic_shared_areas_and_skewed_topics(
    tmp_path,
):
    store, fabric, _, compiler = T.setup(tmp_path)
    context = compiler.context
    for i in range(4):
        context.add_entry(f"영역{i}", "area")
    additions = [
        T.draft(f"other-{i}", "네트워크" if i % 5 else "도구").model_copy(
            update={
                "tags": [f"영역{i % 4}", "네트워크" if i % 5 else "도구"],
                "counterpart": f"상대{i % 9}",
                "content": "합성 근거 " * (30 + i % 10),
            }
        )
        for i in range(200)
    ]
    fabric.append(additions)
    head = store._git("rev-parse", "HEAD")
    before = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in (store.root).rglob("*")
        if p.is_file()
    }
    readonly = WikiStore.open_readonly(store.vault, store.db_path.parent)
    result = measure(
        readonly, fabric.entries(ThreadQuery()), context.snapshot(), after_seq=2
    )
    assert result["llm_calls"] == 0 and result["cycles"]
    print(
        "REALISTIC_MEASUREMENT "
        + json.dumps(
            [
                {
                    "old": cycle["old_full"]["estimated_tokens"],
                    "new": cycle["new_partial"]["estimated_tokens"],
                    "sections": sum(
                        len(keys) for keys in cycle["selected_sections"].values()
                    ),
                }
                for cycle in result["cycles"]
            ]
        )
    )
    assert all(
        cycle["new_partial"]["estimated_tokens"] < cycle["old_full"]["estimated_tokens"]
        for cycle in result["cycles"]
    )
    assert head == store._git("rev-parse", "HEAD")
    assert before == {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }


def test_two_failed_section_attempts_mark_review_pending_and_advance(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    model.extra_section = True
    fabric.append([T.draft("three", "네트워크")])
    from weaver.wiki.batch import WikiValidationError

    before = store.read(T.PATH)[1]
    with pytest.raises(WikiValidationError):
        compiler.run()
    assert compiler.bookmark() == 2
    compiler.run()
    assert compiler.bookmark() == 3
    assert store.metadata(T.PATH)["sections"]["## 네트워크"]["status"] == "검토 대기"
    assert store.read(T.PATH)[1] == before


def test_model_cannot_change_existing_frontmatter(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    props, body, base = store.read(T.PATH)
    store.write(
        T.PATH,
        props | {"aliases": ["사람 별칭"], "tags": ["사람 태그"], "next": "직접 확인"},
        body,
        base,
    )
    original = model.generate_json

    def injected(task, system, user, schema):
        result = original(task, system, user, schema)
        if task == "wiki.compile_batch":
            result.pages[0].aliases = ["모델이 바꾼 별칭"]
        return result

    model.generate_json = injected
    fabric.append([T.draft("three", "네트워크")])
    compiler.run()
    props = store.read(T.PATH)[0]
    assert props["aliases"] == ["사람 별칭"] and props["next"] == "직접 확인"
    assert props["tags"] == ["사람 태그"] and props["area"] == "업무 운영"


def test_large_section_uses_merge_only_with_old_body_and_new_evidence(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    more = fabric.append([T.draft(f"old-{i}", "네트워크") for i in range(65)])
    meta = store.metadata(T.PATH)
    mapping = meta["sections"]
    mapping["## 네트워크"]["thread_ids"] = ["one", *(t.thread_id for t in more)]
    store.write_metadata(T.PATH, {"sections": mapping})
    with sqlite3.connect(compiler.state_path) as db:
        db.execute("UPDATE bookmark SET seq=67")
    model.calls.clear()
    fabric.append([T.draft("new", "네트워크")])
    compiler.run()
    payload = model.calls[0][1]
    assert payload["merge_only"] == {T.PATH: ["## 네트워크"]}
    assert [t["thread_id"] for t in payload["threads"]] == ["new"]
    assert (
        "초기 상태" in store.read(T.PATH)[1] and "⚠ 최신 기록:" in store.read(T.PATH)[1]
    )
    assert set(store.metadata(T.PATH)["sections"]["## 네트워크"]["thread_ids"]) >= {
        "old-0",
        "old-64",
        "new",
    }


def test_full_page_fallback_when_more_than_half_sections_selected(tmp_path, caplog):
    store, fabric, model, compiler = T.setup(tmp_path)
    mapping = store.metadata(T.PATH)["sections"]
    mapping["## 도구"]["thread_ids"] = ["one"]
    mapping["## 도구"]["tags"] = ["네트워크"]
    store.write_metadata(T.PATH, {"sections": mapping})
    fabric.append([T.draft("three", "네트워크")])
    with caplog.at_level("INFO"):
        compiler.run()
    payload = model.calls[-1][1]
    assert payload["compile_modes"][T.PATH] == "full"
    assert "exceed half" in caplog.text


def test_unclassified_queue_waits_for_three_threads_or_seven_days(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    model.calls.clear()
    fabric.append([T.draft("u1", "아직 없는 주제")])
    compiler.run()
    assert model.calls == [] and compiler.bookmark() == 3
    fabric.append([T.draft("u2", "아직 없는 주제")])
    compiler.run()
    assert model.calls == [] and compiler.bookmark() == 4

    # A fake model can decline a new page while accounting for every queued ID.
    def decline(task, system, user, schema):
        payload = json.loads(user)
        model.calls.append((task, payload))
        return schema.model_validate(
            {
                "pages": [],
                "excluded_threads": [
                    {"thread_id": t["thread_id"], "reason": "중복"}
                    for t in payload["threads"]
                ],
            }
        )

    model.generate_json = decline
    fabric.append([T.draft("u3", "아직 없는 주제")])
    compiler.run()
    assert {t["thread_id"] for t in model.calls[-1][1]["threads"]} == {"u1", "u2", "u3"}
    fabric.append([T.draft("u4", "다른 미분류")])
    compiler.run()
    before = len(model.calls)
    store.clock = lambda: datetime.now(UTC).timestamp() + 8 * 86400
    compiler.run()
    assert len(model.calls) == before + 1


def test_new_sections_can_be_published_without_rewriting_old_sections(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    before = {p.key: p.text for p in sections(store.read(T.PATH)[1])}

    def add_section(task, system, user, schema):
        return schema.model_validate(
            {
                "pages": [],
                "excluded_threads": [],
                "new_sections": [
                    {
                        "page": T.PATH,
                        "after_section": "## 네트워크",
                        "body": "## 새 조치\n연결 상태를 확인한다.\n",
                    }
                ],
            }
        )

    model.generate_json = add_section
    fabric.append([T.draft("three", "네트워크")])
    compiler.run()
    after = {p.key: p.text for p in sections(store.read(T.PATH)[1])}
    assert "## 새 조치" in after and "## 새 조치" in store.metadata(T.PATH)["sections"]
    assert after["## 도구"] == before["## 도구"]
    assert after[MEMO] == before[MEMO]


def test_migration_seeds_surviving_human_sentences_from_git(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    file, baseline = legacy(store)
    human = _render({"thread_ids": ["one"]}, "# 운영\n\n## 현재\n사람이 남긴 판단\n")
    file.write_bytes(human)
    rev = store._commit(T.PATH, human, "human", "processed human edit")
    with store._db() as db:
        db.execute("UPDATE pages SET content=?, rev=?", (human, rev))
    migration = WikiMigration(store, [])
    plan = migration.preview()
    spans = plan["changes"][0]["metadata"]["human_spans"]
    assert any("사람이 남긴 판단" in span["text"] for span in spans)


def test_evidence_is_reinserted_but_human_created_evidence_is_not_erased(tmp_path):
    store, _, model, compiler = T.setup(tmp_path)
    T.settle_edit(
        store, compiler, lambda text: text.replace("## 근거\n", "## 바꾼 근거\n")
    )
    assert "## 근거" in store.read(T.PATH)[1]
    other = "pages/사람 문서.md"
    store.write(other, {}, "# 사람 문서\n본문\n", None)
    page = store.root / other
    page.write_text(
        page.read_text().replace(MEMO, "## 근거\n사람이 만든 근거\n\n" + MEMO)
    )
    store.scan(store.clock())
    edits = store.scan(store.clock() + 1)
    for edit in edits:
        compiler._edit(edit)
    assert "사람이 만든 근거" in store.read(other)[1]


def test_memo_variants_renames_and_order_moves_are_code_only(tmp_path):
    store, _, model, compiler = T.setup(tmp_path)
    model.calls.clear()
    T.settle_edit(store, compiler, lambda text: text.replace(MEMO, "## 내   메모 ##"))
    assert model.calls == []
    T.settle_edit(
        store, compiler, lambda text: text.replace("## 내   메모 ##", "## 개인 생각")
    )
    assert any(part.key == MEMO for part in sections(store.read(T.PATH)[1]))
    assert model.calls == []


def test_wiki_measure_cli_does_not_initialize_stores_or_call_models(
    tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    from weaver.app import cli
    from weaver.config import load_config

    config = tmp_path / "server.toml"
    config.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
        + "\n[wiki]\nenabled = true\n"
    )
    settings = load_config(config, init_directories=True)
    ContextDictionary(settings.state_root)
    fabric = FabricStore(settings.state_root / "fabric.sqlite3")
    fabric.append([T.draft("one", "네트워크")])
    store = WikiStore(settings.vault_root, settings.state_root, settle_seconds=0)
    store.write(T.PATH, {}, "# 운영\n\n## 현재\n본문\n", None)

    def forbidden(*args, **kwargs):
        raise AssertionError("measurement must not initialize a writable store")

    monkeypatch.setattr(WikiStore, "__init__", forbidden)
    result = CliRunner().invoke(cli, ["wiki-measure", "--config", str(config)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["dry_run"] and data["llm_calls"] == 0 and len(data["cycles"]) == 1


def test_drifted_scope_keys_are_removed_and_unused_intro_is_rejected(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    meta = store.metadata(T.PATH)
    meta["sections"]["## 사라진 제목"] = dict(meta["sections"]["## 네트워크"])
    store.write_metadata(T.PATH, {"sections": meta["sections"]})
    original = model.generate_json

    def unused(task, system, user, schema):
        result = original(task, system, user, schema)
        if task == "wiki.compile_batch":
            assert "## 사라진 제목" not in json.loads(user)["section_scope"][T.PATH]
            result.pages[0].body = result.pages[0].body.replace(
                "# 운영\n", "# 운영\n버리면 안 되는 출력\n"
            )
        return result

    model.generate_json = unused
    fabric.append([T.draft("three", "네트워크")])
    from weaver.wiki.batch import WikiValidationError

    with pytest.raises(WikiValidationError, match="unused"):
        compiler.run()
    assert compiler.bookmark() == 2


def test_digest_uses_full_period_and_promotion_validation_uses_full_active_log(
    tmp_path, monkeypatch
):
    from zoneinfo import ZoneInfo

    from weaver.wiki.digest import DigestBuilder
    from weaver.wiki.promotion import ProjectLifecycle

    store, fabric, model, compiler = T.setup(tmp_path)
    compiler.digest = DigestBuilder(
        store, tmp_path / "digest.db", ZoneInfo("UTC"), lambda _: "summaries/합성"
    )
    observed = []
    original_apply = ProjectLifecycle.apply

    def full_snapshot(self, candidates, active, *args, **kwargs):
        observed.append({t.thread_id for t in active})
        return original_apply(self, candidates, active, *args, **kwargs)

    monkeypatch.setattr(ProjectLifecycle, "apply", full_snapshot)
    fabric.append([T.draft("three", "네트워크")])
    compiler.run()
    payload = model.calls[-1][1]
    assert {t["thread_id"] for t in payload["threads"]} == {"one", "three"}
    daily = next(
        target for target in payload["digest_targets"] if target["period"] == "daily"
    )
    assert {t["thread_id"] for t in daily["inputs"]["threads"]} == {
        "one",
        "two",
        "three",
    }
    assert observed[-1] == {"one", "two", "three"}


def test_hunk_history_records_produced_thread_identifiers(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    model.verdict = "fact"
    edit = T.settle_edit(
        store,
        compiler,
        lambda text: text.replace("초기 상태", "확인한 새 사실").replace(
            "---\n", '---\ndeadline: "2026-12-01"\n', 1
        ),
    )
    with sqlite3.connect(compiler.state_path) as db:
        rows = [
            json.loads(raw)
            for (raw,) in db.execute(
                "SELECT payload FROM edit_hunks WHERE rev=?", (edit.new_rev,)
            )
        ]
    recorded_ids = {key for row in rows for key in row.get("thread_ids", [])}
    actual_ids = {
        t.thread_id for t in fabric.entries(ThreadQuery()) if t.actor == "human"
    }
    assert recorded_ids == actual_ids and len(actual_ids) == 2


def test_known_page_connection_key_edit_cannot_steal_other_metadata(tmp_path):
    store, _, model, compiler = T.setup(tmp_path)
    other = "pages/다른 문서.md"
    store.write(other, {}, "# 다른 문서\n본문\n", None)
    first_id, other_id = store.read(T.PATH)[0]["weaver"], store.read(other)[0]["weaver"]
    T.settle_edit(store, compiler, lambda text: text.replace(first_id, other_id))
    assert store.read(T.PATH)[0]["weaver"] == first_id
    assert store.read(other)[0]["weaver"] == other_id


def test_renamed_memo_with_new_private_text_never_enters_model_input(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    model.calls.clear()
    T.settle_edit(
        store,
        compiler,
        lambda text: text.replace(MEMO, "## 비공개 생각") + "개인적인 새 판단\n",
    )
    body = store.read(T.PATH)[1]
    assert body.index("개인적인 새 판단") > body.index(MEMO)
    assert model.calls == []
    fabric.append([T.draft("three", "네트워크")])
    compiler.run()
    assert "개인적인 새 판단" not in json.dumps(model.calls, ensure_ascii=False)
    assert store.read(T.PATH)[1].endswith("개인적인 새 판단\n")


def test_full_page_fallback_counts_full_digest_input_above_60k(tmp_path, caplog):
    from zoneinfo import ZoneInfo

    from weaver.wiki.digest import DigestBuilder

    store, fabric, model, compiler = T.setup(tmp_path)
    fabric.append(
        [
            T.draft(f"large-{i}", "다른 주제").model_copy(
                update={"content": "synthetic " * 1000}
            )
            for i in range(30)
        ]
    )
    with sqlite3.connect(compiler.state_path) as db:
        db.execute("UPDATE bookmark SET seq=32")
    compiler.digest = DigestBuilder(
        store, tmp_path / "digest.db", ZoneInfo("UTC"), lambda _: "summaries/합성"
    )
    fabric.append([T.draft("three", "네트워크")])
    with caplog.at_level("INFO"):
        compiler.run()
    payload = model.calls[-1][1]
    assert payload["compile_modes"][T.PATH] == "full"
    assert "60000" in caplog.text
    assert {"one", "two", "three"} <= {t["thread_id"] for t in payload["threads"]}


def test_quote_strike_and_latin_spacing_content_require_one_model_call(tmp_path):
    store, _, model, compiler = T.setup(tmp_path)
    model.calls.clear()
    T.settle_edit(
        store, compiler, lambda text: text.replace("초기 상태", "> ~~server one~~")
    )
    assert len(model.calls) == 1
    model.calls.clear()
    T.settle_edit(store, compiler, lambda text: text.replace("server one", "serverone"))
    assert len(model.calls) == 1


def test_metadata_git_failure_is_not_reported_as_missing(tmp_path, monkeypatch):
    import subprocess

    store, _, _, _ = T.setup(tmp_path)
    original = store._git

    def broken(*args, **kwargs):
        if args[0] == "show":
            raise subprocess.CalledProcessError(128, "git show")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_git", broken)
    with pytest.raises(subprocess.CalledProcessError):
        store.metadata(T.PATH)


def test_order_only_moves_are_recorded_in_code(tmp_path):
    store, _, model, compiler = T.setup(tmp_path)
    parts = {part.key: part.text for part in sections(store.read(T.PATH)[1])}
    model.calls.clear()
    edit = T.settle_edit(
        store,
        compiler,
        lambda text: text.replace(
            parts["## 네트워크"] + parts["## 도구"],
            parts["## 도구"] + parts["## 네트워크"],
        ),
    )
    assert model.calls == [] and ("structure",) in T.records(compiler, edit.new_rev)
    assert store.metadata(T.PATH)["sections"]["## 네트워크"]["thread_ids"] == ["one"]
    assert sections("##내메모\n비공개\n")[0].key == MEMO


def test_query_and_digest_use_the_same_original_summary_label(tmp_path):
    from zoneinfo import ZoneInfo

    from weaver.wiki.digest import DigestBuilder, DigestEntry
    from weaver.wiki.links import summary_path

    store, fabric, _, compiler = T.setup(tmp_path)
    title = "11/18 합성 통화"
    path = summary_path(title, T.SOURCE).removesuffix(".md")
    link = f"[[{path}|{title}]]"
    fabric.append(
        [
            T.draft("request", "네트워크").model_copy(
                update={"kind": "요청", "direction": "받음"}
            )
        ]
    )
    write_pages(
        fabric,
        compiler.context,
        store,
        now=datetime(2026, 11, 19, tzinfo=UTC),
        source_link=lambda _: path,
        source_label=lambda _: title,
    )
    assert link in store.read("열린 요청.md")[1]
    builder = DigestBuilder(
        store,
        tmp_path / "digest.db",
        ZoneInfo("UTC"),
        lambda _: path,
        source_label=lambda _: title,
    )
    targets = builder.plan(fabric.entries(ThreadQuery()), 3)
    entries = [
        DigestEntry.model_validate(
            {
                "path": target.path,
                "sentences": [{"text": "합성 요약", "evidence": target.evidence}],
            }
        )
        for target in targets
    ]
    builder.apply(targets, entries, 3)
    assert link in store.read(targets[0].path)[1]
    assert link in store.read(T.PATH)[1]


def test_migration_does_not_assign_shared_area_to_every_section(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    legacy(store, body="# 운영\n\n## 첫째\n업무 운영\n\n## 둘째\n업무 운영\n")
    fabric = FabricStore(tmp_path / "fabric.db")
    facts = fabric.append([T.draft("one", "네트워크"), T.draft("two", "도구")])
    context = ContextDictionary(tmp_path / "context")
    context.add_entry("업무 운영", "area")
    plan = WikiMigration(store, facts, context.snapshot()).preview()
    mapping = plan["changes"][0]["metadata"]["sections"]
    assert all(
        not data["thread_ids"] and data["status"] == "미분류"
        for data in mapping.values()
    )


def test_page_without_metadata_gets_heading_bootstrap(tmp_path):
    store, _, model, _ = T.setup(tmp_path)
    path = "pages/기존 사람 문서.md"
    file = store._path(path)
    file.write_text("# 기존 사람 문서\n\n## 현재\n현재 문장\n\n## 다음\n다음 문장\n")
    model.calls.clear()
    store.scan(store.clock())
    mapping = store.metadata(path)["sections"]
    assert set(mapping) == {"__intro__", "## 현재", "## 다음"}
    assert all(data["thread_ids"] == [] for data in mapping.values())
    assert model.calls == []


def test_heading_rename_and_fact_edit_split_code_structure_from_model_content(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    model.calls.clear()
    model.verdict = "fact"
    edit = T.settle_edit(
        store,
        compiler,
        lambda text: text.replace("## 네트워크", "## 네트워크 구성").replace(
            "초기 상태", "연결 정책을 변경했다"
        ),
    )
    assert len(model.calls) == 1
    assert len(model.calls[0][1]["hunks"]) == 1
    assert model.calls[0][1]["hunks"][0]["section"] == "## 네트워크 구성"
    assert "초기 상태" in model.calls[0][1]["hunks"][0]["before"]
    mapping = store.metadata(T.PATH)["sections"]
    assert "## 네트워크" not in mapping and mapping["## 네트워크 구성"][
        "thread_ids"
    ] == ["one"]
    assert ("structure",) in T.records(compiler, edit.new_rev)
    assert len([t for t in fabric.entries(ThreadQuery()) if t.actor == "human"]) == 1


def test_each_fact_hunk_records_only_its_own_produced_threads(tmp_path):
    store, _, model, compiler = T.setup(tmp_path)
    model.verdict = "fact"
    edit = T.settle_edit(
        store,
        compiler,
        lambda text: text.replace("초기 상태", "새 네트워크 사실").replace(
            "사람이 고른 문장을 그대로 둡니다.", "새 도구 사실"
        ),
    )
    with sqlite3.connect(compiler.state_path) as db:
        records = [
            json.loads(raw)
            for (raw,) in db.execute(
                "SELECT payload FROM edit_hunks WHERE rev=? AND classification='fact' ORDER BY hunk",
                (edit.new_rev,),
            )
        ]
    assert len(records) == 2
    assert all(len(record["thread_ids"]) == 1 for record in records)
    assert set(records[0]["thread_ids"]).isdisjoint(records[1]["thread_ids"])
    compiler._edit(edit)
    with sqlite3.connect(compiler.state_path) as db:
        retried = [
            json.loads(raw)
            for (raw,) in db.execute(
                "SELECT payload FROM edit_hunks WHERE rev=? AND classification='fact' ORDER BY hunk",
                (edit.new_rev,),
            )
        ]
    assert [row["thread_ids"] for row in retried] == [
        row["thread_ids"] for row in records
    ]


def test_promotion_prompt_uses_compact_full_cluster_summary(tmp_path):
    _, fabric, _, compiler = T.setup(tmp_path)
    historic = fabric.append(
        [
            T.draft(f"prior-{i}", "네트워크").model_copy(
                update={"content": "긴 과거 근거 " * 100}
            )
            for i in range(100)
        ]
    )
    newest = fabric.append(
        [
            T.draft("new", "네트워크").model_copy(
                update={"source_id": "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"}
            )
        ]
    )
    candidates = compiler._promotion_candidates(fabric.entries(ThreadQuery()), newest)
    target = next(item for item in candidates if item["key"] == "네트워크")
    assert target["evidence_count"] == len(historic) + 2
    assert target["source_count"] == 2 and len(target["evidence_ids"]) == 8
    assert len(target["summary"]) <= 323


def test_section_cap_counts_old_and_new_evidence_together(tmp_path):
    store, fabric, model, compiler = T.setup(tmp_path)
    older = fabric.append([T.draft(f"prior-{i}", "네트워크") for i in range(62)])
    mapping = store.metadata(T.PATH)["sections"]
    mapping["## 네트워크"]["thread_ids"] = [
        "one",
        *(thread.thread_id for thread in older),
    ]
    store.write_metadata(T.PATH, {"sections": mapping})
    with sqlite3.connect(compiler.state_path) as db:
        db.execute("UPDATE bookmark SET seq=64")
    fabric.append([T.draft("new-a", "네트워크"), T.draft("new-b", "네트워크")])
    compiler.run()
    payload = model.calls[-1][1]
    assert payload["merge_only"] == {T.PATH: ["## 네트워크"]}
    assert {thread["thread_id"] for thread in payload["threads"]} == {"new-a", "new-b"}
    assert "초기 상태" in store.read(T.PATH)[1]
    assert len(store.metadata(T.PATH)["sections"]["## 네트워크"]["thread_ids"]) == 65


@pytest.mark.parametrize("anchor", [MEMO, "## 근거"])
def test_new_sections_cannot_be_inserted_inside_personal_or_managed_tail(
    tmp_path, anchor
):
    store, fabric, model, compiler = T.setup(tmp_path)
    before = store.read(T.PATH)[1]

    def unsafe(task, system, user, schema):
        return schema.model_validate(
            {
                "pages": [],
                "excluded_threads": [],
                "new_sections": [
                    {
                        "page": T.PATH,
                        "after_section": anchor,
                        "body": "## 새 섹션\n잘못된 위치\n",
                    }
                ],
            }
        )

    model.generate_json = unsafe
    fabric.append([T.draft("three", "네트워크")])
    from weaver.wiki.batch import WikiValidationError

    with pytest.raises(WikiValidationError, match="new wiki section"):
        compiler.run()
    assert store.read(T.PATH)[1] == before and compiler.bookmark() == 2
