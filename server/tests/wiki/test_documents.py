"""Bundled PARA documents with fixture LLM responses and tmp vaults only."""

import json
import logging
import math
import time
import unicodedata
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from weaver.context import ContextDictionary
from weaver.context.tree import TreeNode, node_kind
from weaver.fabric import FabricStore
from weaver.llm.errors import SchemaMismatch
from weaver.reference.index import RefChunk
from weaver.schema import ThreadDraft
from weaver.wiki import layout
from weaver.wiki.compiler import Summary
from weaver.wiki.digest import DigestBuilder
from weaver.wiki.documents import DocumentBatch, DocumentCompiler
from weaver.wiki.store import WikiStore

SRC = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
ZONE = ZoneInfo("Asia/Seoul")
# Far enough ahead of any file mtime that the settle window is never open.
FIXED_NOW = datetime(2030, 1, 1, 3, tzinfo=UTC).timestamp()
AT = datetime(2026, 11, 20, 3, tzinfo=UTC)
ROUTER = "회사/infra/network/router"
PROJECT = "회사/project/26-12-라우터-교체"
NETWORK_PAGE = "pages/회사/infra/network.md"
PROJECT_PAGE = "pages/회사/project/26-12-라우터-교체.md"
TREE = [
    "회사",
    "회사/infra",
    "회사/infra/network",
    ROUTER,
    "회사/project",
    PROJECT,
    "회사/미분류",
]


def draft(tid, tags, kind="기록", at=AT, **kw):
    data = dict(
        thread_id=tid,
        kind=kind,
        content=f"사실 {tid}",
        occurred_at=at,
        recorded_at=at,
        tags=list(tags),
        field=None,
        value=None,
        source_id=SRC,
        actor="ai_approved",
        direction="받음" if kind == "요청" else None,
        counterpart="홍길동" if kind == "요청" else None,
        resolves=None,
        retracts=None,
        correction=None,
    )
    data.update(kw)
    return ThreadDraft.model_validate(data)


def request(tid, tags, direction="받음", deadline=None, **kw):
    extra = {"field": "deadline", "value": deadline} if deadline else {}
    return draft(tid, tags, "요청", direction=direction, **extra, **kw)


class Llm:
    def __init__(self):
        self.calls = []
        self.edit = None
        self.fail = False

    def generate_json(self, task, system, user, schema):
        payload = json.loads(user)
        self.calls.append((task, payload, user))
        if self.fail:
            raise SchemaMismatch()
        data = {
            "documents": [
                {
                    "key": doc["key"],
                    "title": doc["title"],
                    "current": [
                        {"text": f"현황 {t['id']}", "thread_ids": [t["id"]]}
                        for t in doc["threads"][:2]
                    ],
                    "cautions": [],
                }
                for doc in payload["documents"]
            ],
            "digests": [
                {
                    "path": t["path"],
                    "sentences": [{"text": "요약", "evidence": t["evidence"]}],
                }
                for t in payload["digest_targets"]
            ],
        }
        if self.edit is not None:
            data = self.edit(payload, data)
        return schema.model_validate(data)

    def generate_text(self, *args):
        raise AssertionError("documents use one structured call")


def setup(tmp_path, references=None, directives=None, settle=0, clock=None):
    store = WikiStore(
        tmp_path / "vault",
        tmp_path / "state",
        settle_seconds=settle,
        clock=clock or (lambda: FIXED_NOW),
    )
    fabric = FabricStore(tmp_path / "fabric.db")
    context = ContextDictionary(tmp_path / "context")
    context.sync_tree("회사", [TreeNode(n, node_kind(n)) for n in TREE])
    llm = Llm()

    def no_summary(_):
        raise LookupError("no summary")

    kwargs = {}
    if references is not None:
        kwargs["references"] = references
    if directives is not None:
        kwargs["directives"] = directives
    compiler = DocumentCompiler(
        store,
        fabric,
        context,
        llm,
        tmp_path / "compiler.db",
        no_summary,
        **kwargs,
    )
    compiler.timezone = ZONE
    return store, fabric, llm, compiler, context


def pages(store):
    return {
        unicodedata.normalize("NFC", p.relative_to(store.root).as_posix())
        for p in (store.root / "pages").rglob("*.md")
    }


def text(store, path):
    return store.read(path)[1]


def test_pages_only_for_para_keys(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append(
        [
            draft("r1", [ROUTER]),
            draft("r2", [ROUTER]),
            draft("p1", [PROJECT]),
            draft("u1", ["회사/미분류"]),
        ]
    )
    compiler.run()
    assert pages(store) == {NETWORK_PAGE, PROJECT_PAGE}
    assert [task for task, *_ in llm.calls] == ["wiki.compile_documents"]
    keys = {d["key"] for d in llm.calls[0][1]["documents"]}
    assert keys == {"회사/infra/network", PROJECT}
    meta = store.metadata(PROJECT_PAGE)
    assert meta["page_key"] == PROJECT and meta["title"] == "라우터 교체"
    assert meta["kind"] == "project" and meta["source_ids"] == [SRC]
    assert meta["as_of"] == "2030-01-01"


def test_document_layout_and_code_sections(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append(
        [
            request("mine", [PROJECT], deadline="2026-11-30"),
            request("theirs", [PROJECT], direction="보냄"),
        ]
    )
    compiler.run()
    body = text(store, PROJECT_PAGE)
    order = [body.index(h) for h in ("## 현재 상태", "## 남은 일", "## 기다리는 중")]
    assert order == sorted(order)
    mine = layout.task_line(
        "사실 mine", layout.block_id("mine"), deadline=date(2026, 11, 30), done=None
    )
    waiting = layout.task_line(
        "사실 theirs · 홍길동", layout.block_id("theirs"), deadline=None, done=None
    )
    assert mine in body and waiting in body
    # The model never wrote a remaining-work sentence.
    assert "사실 mine" not in json.dumps(llm.calls[0][1]["documents"][0]["bullets"])


def test_bullet_with_foreign_thread_id_is_dropped(tmp_path, caplog):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("r1", [ROUTER]), draft("p1", [PROJECT])])

    def edit(payload, data):
        for doc in data["documents"]:
            if doc["key"] == "회사/infra/network":
                doc["current"] = [
                    {"text": "라우터 점검 완료", "thread_ids": ["r1"]},
                    {"text": "다른 문서의 사실", "thread_ids": ["p1"]},
                ]
        return data

    llm.edit = edit
    with caplog.at_level(logging.WARNING):
        compiler.run()
    body = text(store, NETWORK_PAGE)
    assert "라우터 점검 완료" in body and "다른 문서의 사실" not in body
    meta = store.metadata(NETWORK_PAGE)
    assert [b["thread_ids"] for b in meta["bullets"]] == [["r1"]]
    assert meta["bullets"][0]["bullet_id"] == "b1"
    assert meta["bullets"][0]["section"] == "current"
    assert "document bullet rejected: foreign evidence" in caplog.text


def test_all_bullets_invalid_keeps_previous_version(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("r1", [ROUTER]), draft("p1", [PROJECT])])
    compiler.run()
    before = {p: (store.root / p).read_bytes() for p in (NETWORK_PAGE, PROJECT_PAGE)}
    fabric.append([draft("r2", [ROUTER])])
    llm.edit = lambda payload, data: {
        **data,
        "documents": [
            {
                "key": d["key"],
                "title": d["title"],
                "current": [{"text": "가짜 근거", "thread_ids": ["p1", "nope"]}],
            }
            for d in data["documents"]
        ],
    }
    compiler.run()
    assert len(llm.calls) == 2
    assert (store.root / NETWORK_PAGE).read_bytes() == before[NETWORK_PAGE]
    assert (store.root / PROJECT_PAGE).read_bytes() == before[PROJECT_PAGE]


def test_caution_section_only_with_candidates(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([request("base", [ROUTER], deadline="2026-11-25")])
    compiler.run()
    assert "## 주의할 이력" not in text(store, NETWORK_PAGE)
    day = datetime(2026, 11, 21, 3, tzinfo=UTC)
    fabric.append(
        [
            draft(
                "amend",
                [ROUTER],
                "결정",
                at=day,
                actor="human",
                field="deadline",
                value="2026-12-05",
                amends="base",
            )
        ]
    )

    def edit(payload, data):
        candidates = payload["documents"][0]["caution_candidates"]
        data["documents"][0]["cautions"] = [
            {"candidate_id": candidates[0]["candidate_id"], "text": "기한 연기"},
            {"candidate_id": "c-unknown0", "text": "버려질 문장"},
        ]
        return data

    llm.edit = edit
    compiler.run()
    body = text(store, NETWORK_PAGE)
    assert "## 주의할 이력" in body
    assert "- **11/21** [기한 변경] 기한 연기" in body
    assert "버려질 문장" not in body
    assert [c["text_sha256"] for c in store.metadata(NETWORK_PAGE)["cautions"]]


def test_references_are_links_without_body(tmp_path):
    seen = []

    def references(key, title):
        seen.append((key, title))
        return [
            layout.RefLink("home", "3. Resource/네트워크/라우터 설정", "설정", True),
            layout.RefLink("회사", "network/router.md", "설정 절차", False),
        ]

    store, fabric, llm, compiler, _ = setup(tmp_path, references=references)
    fabric.append([draft("r1", [ROUTER])])
    compiler.run()
    body = text(store, NETWORK_PAGE)
    assert "[[3. Resource/네트워크/라우터 설정|설정]]" in body
    assert "`회사 vault`" in body
    assert seen == [("회사/infra/network", "Network")]
    assert "라우터 설정" not in llm.calls[0][2]


def test_default_references_drop_chunk_bodies(tmp_path, monkeypatch):
    secret = "참고자료 본문 비밀문장"
    chunks = [
        RefChunk("home", "3. Resource/네트워크/설정.md", "설정", 0, secret,
                 (), "3. Resource", (), None, 1.0, "tag"),
        RefChunk("회사", "network/router.md", "절차", 0, secret,
                 (), "network", (), "abc123", 1.0, "tag"),
    ]  # fmt: skip
    called = []

    def fake(root, tags, query, limit):
        called.append((root, tags, query, limit))
        return chunks

    monkeypatch.setattr("weaver.wiki.documents.find_references", fake)
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("r1", [ROUTER])])
    compiler.run()
    body = text(store, NETWORK_PAGE)
    assert "[[3. Resource/네트워크/설정|설정]]" in body
    assert "`회사 vault` network/router.md › 절차" in body
    assert secret not in body and secret not in llm.calls[0][2]
    assert called[0][1:] == (["회사/infra/network"], "Network", 5)


def test_unchanged_inputs_no_llm_but_remaining_rerenders(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([request("ask", [PROJECT])])
    compiler.run()
    block = layout.block_id("ask")
    assert f"^{block}" in text(store, PROJECT_PAGE)
    fabric.append([draft("done", [PROJECT], "결정", actor="human", resolves="ask")])
    compiler.run()
    assert len(llm.calls) == 1
    assert f"^{block}" not in text(store, PROJECT_PAGE)
    compiler.run()
    assert len(llm.calls) == 1


def test_written_readonly(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("r1", [ROUTER])])
    compiler.run()
    path = store.root / NETWORK_PAGE
    original = path.read_bytes()
    path.write_text(path.read_text() + "\n사람이 고친 줄\n")
    store.restore_readonly()
    assert path.read_bytes() == original


def test_validation_clips_per_item_and_falls_back_to_default_title(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft(f"r{i}", [ROUTER]) for i in range(7)])

    def edit(payload, data):
        ids = [t["id"] for t in payload["documents"][0]["threads"]]
        data["documents"][0]["title"] = "나쁜/제목"
        data["documents"][0]["current"] = [
            {"text": "가" * 300, "thread_ids": [ids[0]]},
            *({"text": f"항목 {i}", "thread_ids": [ids[0]]} for i in range(6)),
        ]
        return data

    llm.edit = edit
    compiler.run()
    body = text(store, NETWORK_PAGE)
    assert body.startswith("# Network\n")
    bullets = [line for line in body.splitlines() if line.startswith("- 가")]
    assert bullets == ["- " + "가" * 140]
    assert len(store.metadata(NETWORK_PAGE)["bullets"]) == 5


def test_directives_reach_the_model_and_change_the_identity(tmp_path):
    notes = {"회사/infra/network": ["속도 위주로 쓴다"]}
    store, fabric, llm, compiler, _ = setup(
        tmp_path, directives=lambda key: notes.get(key, [])
    )
    fabric.append([draft("r1", [ROUTER])])
    compiler.run()
    assert llm.calls[0][1]["documents"][0]["directives"] == ["속도 위주로 쓴다"]
    notes["회사/infra/network"] = []
    fabric.append([draft("r2", [ROUTER])])
    compiler.run()
    assert llm.calls[1][1]["documents"][0]["directives"] == []


def test_digest_targets_share_the_single_call(tmp_path):
    store, fabric, llm, compiler, context = setup(tmp_path)
    compiler.digest = DigestBuilder(
        store,
        tmp_path / "digest.db",
        ZONE,
        lambda source: f"Events/{source}",
        dictionary=context.snapshot,
    )
    fabric.append([draft("r1", [ROUTER])])
    compiler.run()
    assert len(llm.calls) == 1
    assert llm.calls[0][1]["digest_targets"]
    assert store.read("Digest/2026/11/2026-11-20.md")
    compiler.run()
    assert len(llm.calls) == 1


def test_wikijob_selects_compiler_by_tree(tmp_path):
    from weaver.runtime.wiki import WikiJob
    from weaver.wiki.batch import BatchCompiler

    def job(root, with_tree):
        context = ContextDictionary(root / "context")
        if with_tree:
            context.sync_tree("회사", [TreeNode(n, node_kind(n)) for n in TREE])
        store = WikiStore(root / "vault", root / "wstate", settle_seconds=0)
        return WikiJob(
            store,
            FabricStore(root / "fabric.db"),
            context,
            Llm(),
            root / "job-state",
            ZONE,
            lambda _: Summary("요약", "본문"),
        )

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert type(job(tmp_path / "a", False).compiler) is BatchCompiler
    tree_job = job(tmp_path / "b", True)
    assert type(tree_job.compiler) is DocumentCompiler
    assert tree_job.compiler.state_path.name == "wiki-documents.sqlite3"
    assert tree_job.compiler.bookmark() == 0


def test_document_models_carry_no_length_or_count_limits():
    schema = json.dumps(DocumentBatch.model_json_schema())
    for word in ("maxLength", "minLength", "minItems", "maxItems"):
        assert word not in schema


@pytest.mark.parametrize("title", ["", "a" * 41, "a^b", "a|b", "[a]", "a\\b", "a#b"])
def test_bad_titles_use_the_default(tmp_path, title):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("p1", [PROJECT])])
    llm.edit = lambda payload, data: {
        **data,
        "documents": [{**data["documents"][0], "title": title}],
    }
    compiler.run()
    assert text(store, PROJECT_PAGE).startswith("# 라우터 교체\n")


def test_settle_window_defers_without_repeated_model_calls(tmp_path):
    now = [time.time()]
    store, fabric, llm, compiler, _ = setup(tmp_path, settle=1800, clock=lambda: now[0])
    fabric.append([draft("r1", [ROUTER])])
    compiler.run()
    assert len(llm.calls) == 1
    fabric.append([draft("r2", [ROUTER]), draft("p1", [PROJECT])])
    for _ in range(3):
        compiler.run()
    # The router file is inside its settle window: only the project is compiled.
    assert [d["key"] for d in llm.calls[1][1]["documents"]] == [PROJECT]
    assert len(llm.calls) == 2
    assert "현황 r2" not in text(store, NETWORK_PAGE)
    assert compiler.bookmark() == 3
    now[0] += 1801
    compiler.run()
    assert len(llm.calls) == 3
    assert "현황 r2" in text(store, NETWORK_PAGE)
    compiler.run()
    assert len(llm.calls) == 3


def test_accepted_result_survives_a_failed_write(tmp_path, monkeypatch):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("r1", [ROUTER])])
    real = store.write

    def busy(*args, **kwargs):
        raise HumanEditPending(args[0])

    from weaver.wiki.store import HumanEditPending

    monkeypatch.setattr(store, "write", busy)
    compiler.run()
    assert len(llm.calls) == 1 and compiler.bookmark() == 1
    assert not (store.root / NETWORK_PAGE).exists()
    monkeypatch.setattr(store, "write", real)
    compiler.run()
    assert len(llm.calls) == 1
    assert "현황 r1" in text(store, NETWORK_PAGE)


def test_permanent_failure_is_retried_once_then_skipped(tmp_path, caplog):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    llm.fail = True
    fabric.append([draft("r1", [ROUTER])])
    with pytest.raises(SchemaMismatch):
        compiler.run()
    assert compiler.bookmark() == 0
    with caplog.at_level(logging.WARNING):
        compiler.run()
    assert "skipped: repeated failure" in caplog.text
    assert compiler.bookmark() == 1
    compiler.run()
    assert len(llm.calls) == 2
    assert not pages(store)


def test_keys_are_split_into_calls_of_eight(tmp_path):
    names = [f"회사/infra/net{i}/router" for i in range(10)]
    store, fabric, llm, compiler, context = setup(tmp_path)
    nodes = [TreeNode(n, node_kind(n)) for n in TREE]
    for i in range(10):
        nodes += [
            TreeNode(f"회사/infra/net{i}", "area"),
            TreeNode(names[i], "topic"),
        ]
    context.sync_tree("회사", nodes)
    fabric.append([draft(f"t{i}", [names[i]]) for i in range(10)])
    compiler.run()
    assert len(llm.calls) == math.ceil(10 / 8)
    assert [len(c[1]["documents"]) for c in llm.calls] == [8, 2]
    assert len(pages(store)) == 10


def test_old_flat_pages_are_retired_and_nested_documents_written(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    store.write(
        "pages/네트워크 운영.md", {"kind": "topic"}, "# 네트워크 운영\n\n서술", None
    )
    store.write("pages/업무 도구.md", {"kind": "topic"}, "# 업무 도구\n\n서술", None)
    store.write("index.md", {}, "# Weaver Wiki\n", None, readonly=True)
    edited = store.root / "pages/업무 도구.md"
    edited.write_text(edited.read_text() + "\n사람이 쓴 문장\n")
    fabric.append([draft("r1", [ROUTER])])
    compiler.run()
    assert not (store.root / "pages/네트워크 운영.md").exists()
    assert not (store.root / "index.md").exists()
    assert edited.exists() and "사람이 쓴 문장" in edited.read_text()
    assert NETWORK_PAGE in pages(store)


def test_dirty_key_without_valid_document_stops_retrying(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("p1", [PROJECT])])
    compiler.run()
    assert len(llm.calls) == 1
    compiler.directives = lambda key: ["나눠 주세요"]
    compiler.mark_dirty(PROJECT)
    llm.edit = lambda payload, data: {**data, "documents": []}
    for _ in range(5):
        compiler.run()
    # The initial compile plus at most MAX_ATTEMPTS dirty retries.
    assert len(llm.calls) <= 3
    assert compiler._sql("SELECT key FROM dirty") == []


def test_dirty_key_with_only_invalid_evidence_is_capped(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    fabric.append([draft("p1", [PROJECT])])
    compiler.run()
    compiler.directives = lambda key: ["나눠 주세요"]
    compiler.mark_dirty(PROJECT)

    def foreign(payload, data):
        for doc in data["documents"]:
            for bullet in doc["current"]:
                bullet["thread_ids"] = ["unknown"]
        return data

    llm.edit = foreign
    for _ in range(5):
        compiler.run()
    assert len(llm.calls) <= 3
    assert compiler._sql("SELECT key FROM dirty") == []


def test_skipped_chunk_digest_is_planned_again_with_next_new_thread(tmp_path):
    store, fabric, llm, compiler, _ = setup(tmp_path)
    compiler.digest = DigestBuilder(
        store,
        tmp_path / "digest.db",
        ZONE,
        lambda s: f"Events/{s}",
        lambda e: None,
    )
    fabric.append([draft("p1", [PROJECT])])
    llm.fail = True
    for _ in range(3):
        try:
            compiler.run()
        except SchemaMismatch:
            pass
    llm.fail = False
    fabric.append([draft("p2", [PROJECT])])
    compiler.run()
    paths = [t["path"] for t in llm.calls[-1][1]["digest_targets"]]
    assert any(path.startswith("Digest/") for path in paths)
