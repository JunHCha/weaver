"""Obsidian treats #, ^ and | as link syntax, even inside filenames."""

import unicodedata

import pytest

from weaver.wiki.links import rewrite_links, summary_path, wikilink
from weaver.wiki.store import WikiStore

SOURCE = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_summary_filename_is_safe_for_obsidian_and_unicode():
    title = unicodedata.normalize("NFD", "2026-11-12 #홍길동 [팀]|회의^ / 통화")
    path = summary_path(title, SOURCE)
    assert path == f"summaries/2026-11-12 -홍길동 -팀--회의- - 통화 {SOURCE}.md"
    assert not any(char in path for char in "#^|[]\\")
    assert unicodedata.is_normalized("NFC", path)
    assert wikilink(path.removesuffix(".md")).endswith(SOURCE + "]]")
    assert wikilink(None) == ""


def test_legacy_rewrite_preserves_alias_and_normalizes_unicode():
    old = "summaries/2026-11-12 #홍길동 call " + SOURCE
    new = summary_path("2026-11-12 #홍길동 call", SOURCE).removesuffix(".md")
    body = f"근거 [[{unicodedata.normalize('NFD', old)}|통화]]와 [[무관]]"
    assert rewrite_links(body, {old: new}) == f"근거 [[{new}|통화]]와 [[무관]]"


def test_repair_only_committed_files_preserves_readonly_and_human_edits(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    for path, readonly in [("pages/주제.md", False), ("조회.md", True)]:
        store.write(path, {}, "[[old]]", None, readonly=readonly)
    store.write("pages/편집중.md", {}, "[[old]]", None)
    target = store.root / "pages/편집중.md"
    target.write_text(target.read_text() + "사람 수정")
    assert store.repair_links({"old": "new"}) == 2
    assert "[[new]]" in store.read("pages/주제.md")[1]
    assert "[[old]]" in target.read_text()
    readonly = store.root / "조회.md"
    readonly.write_text("변경")
    store.restore_readonly()
    assert "[[new]]" in readonly.read_text()
    assert store.repair_links({"old": "new"}) == 0


@pytest.mark.parametrize("actor", ["ai_approved", "human"])
def test_runtime_repairs_existing_hash_links_without_llm(tmp_path, actor):
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from weaver.context import ContextDictionary
    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.runtime.wiki import WikiJob
    from weaver.schema import ThreadDraft
    from weaver.wiki.compiler import Summary

    class Lookup:
        def title(self, source_id):
            if source_id != SOURCE:
                raise LookupError(source_id)
            return "2026-11-12 #홍길동 call"

        def __call__(self, source_id):
            return Summary(self.title(source_id), "승인된 요약")

    class NoLlm:
        def generate_text(self, *args):
            raise AssertionError("repair must not call an LLM")

        def generate_json(self, *args):
            raise AssertionError("repair must not call an LLM")

    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    fabric = FabricStore(tmp_path / "fabric.db")
    fabric.append(
        [
            ThreadDraft(
                thread_id="approved",
                kind="기록",
                content="승인된 사실",
                occurred_at=datetime(2026, 11, 12, tzinfo=UTC),
                recorded_at=datetime(2026, 11, 12, tzinfo=UTC),
                tags=["홍길동"],
                source_id=SOURCE,
                actor=actor,
                field=None,
                value=None,
                direction=None,
                resolves=None,
                retracts=None,
                correction=None,
            )
        ]
    )
    old = f"summaries/2026-11-12 #홍길동 call {SOURCE}"
    store.write(old + ".md", {}, "이전 승인본", None, readonly=True)
    renamed = f"summaries/예전 이름 {SOURCE}.md"
    store.write(renamed, {}, "승인된 요약", None, readonly=True)
    store.write("pages/홍길동.md", {}, f"근거 [[{old}]]", None)
    job = WikiJob(
        store,
        fabric,
        ContextDictionary(tmp_path / "context"),
        NoLlm(),
        tmp_path / "state",
        ZoneInfo("UTC"),
        Lookup(),
    )
    editing = store.root / "pages/홍길동.md"
    baseline = editing.read_bytes()
    editing.write_bytes(baseline + "사람 편집 중".encode())
    job.repair_source_links()
    assert (store.root / (old + ".md")).exists()
    assert "사람 편집 중" in editing.read_text()
    editing.write_bytes(baseline)
    job.repair_source_links()
    target = job.source_link(SOURCE)
    assert target is not None and "#" not in target
    assert (store.root / (target + ".md")).read_text().endswith("승인된 요약\n")
    assert f"[[{target}]]" in store.read("pages/홍길동.md")[1]
    assert not (store.root / (old + ".md")).exists()
    assert not (store.root / renamed).exists()
    assert job.source_link("src_missing") is None
    job.repair_source_links()
    store.restore_readonly()
    assert len(list((store.root / "summaries").glob("*.md"))) == 1
    assert len(fabric.entries(ThreadQuery())) == 1


def test_retired_summary_is_not_restored_after_restart(tmp_path):
    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    old = f"summaries/이전 제목 {SOURCE}.md"
    digest = store.write(old, {}, "요약", None, readonly=True)
    store.retire_summary(old, digest)
    restarted = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    restarted.restore_readonly()
    assert not (store.root / old).exists()
    assert old not in [p for p, _, _ in restarted.published_pages()]
    assert restarted._git("ls-tree", "--name-only", "HEAD", "--", old) == ""


def test_retire_summary_preserves_changed_or_editable_file(tmp_path):
    from weaver.wiki.store import HumanEditPending

    store = WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)
    old = f"summaries/이전 제목 {SOURCE}.md"
    digest = store.write(old, {}, "요약", None, readonly=True)
    (store.root / old).write_text("사람이 추가한 내용")
    with pytest.raises(HumanEditPending):
        store.retire_summary(old, digest)
    assert (store.root / old).read_text() == "사람이 추가한 내용"
    editable = "summaries/직접 작성.md"
    digest = store.write(editable, {}, "사람 메모", None)
    with pytest.raises(HumanEditPending):
        store.retire_summary(editable, digest)
