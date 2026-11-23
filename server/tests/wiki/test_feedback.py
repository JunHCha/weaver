"""Document feedback: fixture LLM, tmp vaults, invented names only."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from weaver.fabric import ThreadQuery
from weaver.llm import policy
from weaver.llm.errors import SchemaMismatch
from weaver.mobile.results import _split_sections
from weaver.runtime.wiki import WikiJob
from weaver.schema.models import FeedbackItem
from weaver.wiki.feedback import (
    FeedbackAlreadyUndone,
    FeedbackProcessor,
    FeedbackStore,
    FeedbackVerdict,
    undo_feedback,
)

from .test_documents import (
    PROJECT,
    PROJECT_PAGE,
    ROUTER,
    SRC,
    draft,
    setup,
    text,
)
from .test_documents import (
    Llm as DocumentLlm,
)

NOW = datetime(2026, 11, 20, 1, 30, tzinfo=UTC)
FACT = "견적 회신은 김철수가 아니라 홍길동 담당"
SPLIT = "회의실 예약 건은 따로 분리"


class FeedbackLlm(DocumentLlm):
    def __init__(self):
        super().__init__()
        self.verdict = None
        self.verdict_fail = False
        self.scopes = []

    def generate_json(self, task, system, user, schema):
        if task != "wiki.feedback":
            return super().generate_json(task, system, user, schema)
        self.calls.append((task, json.loads(user), user))
        self.scopes.append(policy._current.get())
        if self.verdict_fail:
            raise SchemaMismatch()
        return FeedbackVerdict.model_validate(self.verdict)


def fixture(tmp_path):
    store, fabric, llm, compiler, context = setup(tmp_path)
    feedback_llm = FeedbackLlm()
    compiler.llm = llm = feedback_llm
    fabric.append([draft("r1", [PROJECT]), draft("r2", [PROJECT])])
    compiler.run()
    feedback = FeedbackStore(tmp_path / "state" / "wiki-feedback.sqlite3")
    compiler.directives = feedback.directives
    processor = FeedbackProcessor(compiler, feedback, fabric, llm)
    return store, fabric, llm, compiler, feedback, processor


def submit(feedback, text_, key="op1", bullet="b1"):
    return feedback.submit(
        document_id="doc1",
        page_key=PROJECT,
        bullet_id=bullet,
        text=text_,
        chips=["사실이 틀림"],
        client_key=key,
        now=NOW,
    )


def fact_verdict():
    return {
        "classification": "fact",
        "changes": [{"content": FACT, "tags": [PROJECT]}],
    }


def structure_verdict():
    return {"classification": "structure", "directive": SPLIT}


def human_threads(fabric):
    return [t for t in fabric.entries(ThreadQuery()) if t.actor == "human"]


def test_fact_feedback_becomes_human_thread(tmp_path):
    _, fabric, llm, _, feedback, processor = fixture(tmp_path)
    item = submit(feedback, FACT)
    assert item.status == "pending"
    llm.verdict = fact_verdict()
    assert processor.process() == 1
    rows = human_threads(fabric)
    assert len(rows) == 1 and rows[0].content == FACT
    assert rows[0].source_id.startswith("src_")
    done = feedback.get(item.feedback_id)
    assert isinstance(done, FeedbackItem)
    assert done.status == "applied" and done.classification == "fact"
    assert done.thread_ids == [rows[0].thread_id]
    assert done.result and done.result.startswith("정정 1건 반영")
    # The input carries the bullet and its evidence, and the same ID is stable.
    payload = llm.calls[-1][1]
    assert payload["feedback"]["text"] == FACT
    assert payload["threads"]
    other = submit(feedback, FACT, key="op2")
    assert other.feedback_id != item.feedback_id
    assert submit(feedback, "다른 말", key="op1").feedback_id == item.feedback_id


def test_structure_feedback_feeds_next_compile(tmp_path):
    _, fabric, llm, compiler, feedback, processor = fixture(tmp_path)
    item = submit(feedback, SPLIT)
    llm.verdict = structure_verdict()
    processor.process()
    assert feedback.directives(PROJECT) == [SPLIT]
    done = feedback.get(item.feedback_id)
    assert done.classification == "structure" and done.thread_ids == []
    assert done.result == "구성 지시 저장 · 다음 컴파일에 반영"
    before = len(llm.calls)
    compiler.run()
    documents = [c for c in llm.calls[before:] if c[0] == "wiki.compile_documents"]
    assert documents, "the directive must recompile the document"
    assert documents[0][1]["documents"][0]["directives"] == [SPLIT]


def test_undo_retracts_threads_and_directive(tmp_path):
    store, fabric, llm, compiler, feedback, processor = fixture(tmp_path)
    fact = submit(feedback, FACT, key="op1")
    llm.verdict = fact_verdict()
    processor.process()
    compiler.run()
    before = len(llm.calls)
    undone = undo_feedback(feedback, fabric, fact.feedback_id, NOW)
    assert undone.status == "undone"
    created = fact.feedback_id
    retractions = [t for t in fabric.entries(ThreadQuery()) if t.retracts]
    assert [t.thread_id for t in retractions] == [f"feedback-undo:{created}:0"]
    assert retractions[0].actor == "human" and retractions[0].kind == "결정"
    compiler.run()
    documents = [c for c in llm.calls[before:] if c[0] == "wiki.compile_documents"]
    ids = {t["id"] for t in documents[0][1]["documents"][0]["threads"]}
    assert feedback.get(created).thread_ids[0] not in ids
    assert text(store, PROJECT_PAGE)

    structure = submit(feedback, SPLIT, key="op2")
    llm.verdict = structure_verdict()
    processor.process()
    assert feedback.directives(PROJECT) == [SPLIT]
    undo_feedback(feedback, fabric, structure.feedback_id, NOW)
    assert feedback.directives(PROJECT) == []
    processor.process()
    assert feedback.get(structure.feedback_id).status == "undone"


def test_undo_pending_never_processes(tmp_path):
    _, fabric, llm, _, feedback, processor = fixture(tmp_path)
    item = submit(feedback, FACT)
    llm.verdict = fact_verdict()
    before = len(llm.calls)
    undo_feedback(feedback, fabric, item.feedback_id, NOW)
    assert processor.process() == 0
    assert [c for c in llm.calls[before:] if c[0] == "wiki.feedback"] == []
    assert feedback.get(item.feedback_id).status == "undone"
    assert human_threads(fabric) == []


def test_contract_failures_mark_failed_after_two(tmp_path):
    _, _, llm, _, feedback, processor = fixture(tmp_path)
    item = submit(feedback, FACT)
    llm.verdict_fail = True
    for expected in ("pending", "failed", "failed"):
        processor.process()
        assert feedback.get(item.feedback_id).status == expected
    assert [c[0] for c in llm.calls].count("wiki.feedback") == 2


def test_none_verdict_and_long_directive_are_clipped(tmp_path):
    _, fabric, llm, _, feedback, processor = fixture(tmp_path)
    nothing = submit(feedback, "고맙습니다", key="op1")
    llm.verdict = {"classification": "none"}
    processor.process()
    assert feedback.get(nothing.feedback_id).result == "반영할 내용 없음"
    long = submit(feedback, SPLIT, key="op2")
    llm.verdict = {"classification": "structure", "directive": SPLIT * 100}
    processor.process()
    assert len(feedback.directives(PROJECT)[0]) <= 300
    assert feedback.get(long.feedback_id).status == "applied"


def test_directives_keep_latest_five_active(tmp_path):
    _, _, llm, _, feedback, processor = fixture(tmp_path)
    for n in range(7):
        submit(feedback, SPLIT, key=f"op{n}")
        llm.verdict = {"classification": "structure", "directive": f"지시 {n}"}
        processor.process()
    assert feedback.directives(PROJECT) == [f"지시 {n}" for n in range(2, 7)]
    assert feedback.directives(ROUTER) == []


def test_feedback_call_runs_inside_a_provenance_scope(tmp_path):
    _, _, llm, _, feedback, processor = fixture(tmp_path)
    submit(feedback, FACT)
    llm.verdict = fact_verdict()
    processor.process()
    scope = llm.scopes[-1]
    assert scope is not None and scope.shared
    assert SRC in scope.source_ids


def test_one_failing_item_does_not_block_others_or_compile(tmp_path):
    store, fabric, llm, compiler, feedback, processor = fixture(tmp_path)
    first = submit(feedback, "첫째", key="op1")
    second = submit(feedback, "둘째", key="op2")
    llm.verdict = fact_verdict()
    real = fabric.append
    calls = []

    def flaky(drafts):
        calls.append(drafts)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        return real(drafts)

    fabric.append = flaky
    processor.process()
    assert feedback.get(first.feedback_id).status == "failed"
    assert feedback.get(second.feedback_id).status == "applied"
    fabric.append = real
    compiler.run()
    assert text(store, PROJECT_PAGE)


def test_job_swallows_unexpected_feedback_errors():
    class Boom:
        def process(self):
            raise RuntimeError("broken")

    WikiJob._process_feedback(SimpleNamespace(feedback=Boom()))


def test_undo_with_same_key_is_idempotent(tmp_path):
    _, fabric, _, _, feedback, _ = fixture(tmp_path)
    item = submit(feedback, FACT)
    first = undo_feedback(feedback, fabric, item.feedback_id, NOW, "op-a")
    again = undo_feedback(feedback, fabric, item.feedback_id, NOW, "op-a")
    assert first.status == again.status == "undone"
    with pytest.raises(FeedbackAlreadyUndone):
        undo_feedback(feedback, fabric, item.feedback_id, NOW, "op-b")


def test_undoing_a_directive_recompiles_without_it(tmp_path):
    _, fabric, llm, compiler, feedback, processor = fixture(tmp_path)
    item = submit(feedback, SPLIT)
    llm.verdict = structure_verdict()
    processor.process()
    compiler.run()
    undo_feedback(feedback, fabric, item.feedback_id, NOW)
    processor.process()
    before = len(llm.calls)
    compiler.run()
    documents = [c for c in llm.calls[before:] if c[0] == "wiki.compile_documents"]
    assert documents and documents[0][1]["documents"][0]["directives"] == []


def test_bullet_ids_match_the_document_view_numbering(tmp_path):
    store, _, _, compiler, _, _ = fixture(tmp_path)
    stored = compiler.bullets(PROJECT)
    assert len(stored) == 2
    lines = _split_sections(text(store, PROJECT_PAGE))["현재 상태"]
    shown = [x[2:].strip() for x in lines if x.startswith("- ")]
    assert shown == [b.text for b in stored]
    meta = store.metadata(PROJECT_PAGE)["bullets"]
    assert [m["bullet_id"] for m in meta] == ["b1", "b2"]
    assert [m["thread_ids"] for m in meta] == [b.thread_ids for b in stored]


def test_llm_limit_in_feedback_reaches_the_loop_after_compiling(tmp_path):
    from weaver.llm.errors import LlmTimeout

    ran = []

    class Slow:
        def process(self):
            raise LlmTimeout()

    job = SimpleNamespace(
        feedback=Slow(), observer=None, _feedback_error=None, _run=lambda: None
    )

    def run():
        ran.append("compiler")
        WikiJob._process_feedback(job)

    job._run = run
    with pytest.raises(LlmTimeout):
        WikiJob.run(job)
    assert ran == ["compiler"]


def test_failed_apply_after_append_queues_retraction(tmp_path, monkeypatch):
    _, fabric, llm, _, feedback, processor = fixture(tmp_path)
    item = submit(feedback, FACT)
    llm.verdict = fact_verdict()

    def broken(*args, **kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(feedback, "mark", broken)
    assert processor.process() == 0
    failed = feedback.get(item.feedback_id)
    assert failed is not None and failed.status == "failed"
    assert feedback.pending_retractions() == []
    undo = [t for t in fabric.entries(ThreadQuery()) if t.retracts]
    assert len(undo) == 1
    live = [t for t in human_threads(fabric) if not t.retracts]
    assert {t.thread_id for t in live} == {undo[0].retracts}
