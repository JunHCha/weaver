"""Result read models: cards, tasks, tree, documents, evidence, source log.

Every name and value is invented.
"""

import sqlite3
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker

from weaver.context import ContextDictionary
from weaver.context.tree import TreeNode, node_kind
from weaver.schema.models import (
    DocumentSummary,
    DocumentView,
    EventSummary,
    Evidence,
    ResultCard,
    SourceLogEntry,
    TaskItem,
    TreeNodeView,
)
from weaver.wiki import layout
from weaver.wiki.layout import block_id
from weaver.wiki.store import WikiStore

from .helpers import NOW, SID, Harness, schedule, thread

BASE = "/mobile/v1"
READ = f"{BASE}/read"
SRC_B = "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"
NETWORK = "회사/infra/network"
STORAGE = "회사/infra/storage"
TREE = ["회사", "회사/infra", NETWORK, STORAGE, "회사/미분류"]
NOON = "2026-11-20T12:00:00+09:00"


def when(day: str, hour: int = 9) -> datetime:
    return datetime.fromisoformat(f"2026-11-{day}T{hour:02d}:00:00+09:00")


def setup(tmp_path):
    h = Harness(tmp_path)
    ContextDictionary(h.config.state_root).sync_tree(
        "회사", [TreeNode(n, node_kind(n)) for n in TREE]
    )
    return h, h.pair("read")


def state_db(h, name, statements):
    with closing(sqlite3.connect(h.config.state_root / name)) as db, db:
        for sql, args in statements:
            db.execute(sql, args)


def bookmarks(h, documents, digest):
    for name, seq in (
        ("wiki-documents.sqlite3", documents),
        ("wiki-digest.sqlite3", digest),
    ):
        state_db(
            h,
            name,
            [
                (
                    "CREATE TABLE IF NOT EXISTS bookmark (id INTEGER PRIMARY KEY, seq INTEGER NOT NULL)",
                    (),
                ),
                ("INSERT OR REPLACE INTO bookmark VALUES (1, ?)", (seq,)),
            ],
        )


def cards(h, headers, window="today", at=NOON):
    response = h.client.get(
        f"{READ}/cards", headers=headers, params={"window": window, "at": at}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_today_cards_merge_same_case_same_day(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread("a1", tags=[NETWORK], counterpart="김철수", occurred_at=when("20")),
            thread(
                "a2",
                tags=[NETWORK],
                counterpart="이영희",
                source_id=SRC_B,
                occurred_at=when("20", 10),
            ),
            thread("old", tags=[NETWORK], occurred_at=when("19")),
        ]
    )
    body = cards(h, headers)
    assert body["today"] == "2026-11-20" and body["timezone"] == "Asia/Seoul"
    assert body["next_cursor"] is None
    [card] = body["items"]
    assert card["key"] == NETWORK and card["sphere"] == "회사"
    assert card["sources"] == 2 and card["people"] == 2
    assert card["thread_ids"] == ["a1", "a2"]
    assert card["counterparts"] == ["김철수", "이영희"]


def test_unclassified_never_merges(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread("u1", occurred_at=when("20")),
            thread("u2", source_id=SRC_B, occurred_at=when("20", 10)),
        ]
    )
    items = cards(h, headers)["items"]
    assert len(items) == 2
    assert {c["key"] for c in items} == {f"source:{SID}", f"source:{SRC_B}"}
    assert {c["sphere"] for c in items} == {"회사"}


def test_card_text_from_digest_or_fallback_and_reflecting(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread("a1", content="견적서 수령", tags=[NETWORK], occurred_at=when("20")),
            thread(
                "a2", content="장비 목록", tags=[NETWORK], occurred_at=when("20", 10)
            ),
            thread(
                "s1",
                content="용량 증설 문의",
                tags=[STORAGE],
                occurred_at=when("20", 11),
            ),
        ]
    )
    bookmarks(h, documents=2, digest=2)
    state_db(
        h,
        "wiki-digest.sqlite3",
        [
            (
                "CREATE TABLE IF NOT EXISTS cards (day TEXT NOT NULL, key TEXT NOT NULL, "
                "headline TEXT NOT NULL, bullets TEXT NOT NULL, thread_ids TEXT NOT NULL, "
                "PRIMARY KEY (day, key))",
                (),
            ),
            (
                "INSERT INTO cards VALUES (?, ?, ?, ?, ?)",
                (
                    "2026-11-20",
                    NETWORK,
                    "네트워크 견적 도착",
                    '["장비 2종"]',
                    '["a1","a2"]',
                ),
            ),
        ],
    )
    by_key = {c["key"]: c for c in cards(h, headers)["items"]}
    assert by_key[NETWORK]["headline"] == "네트워크 견적 도착"
    assert by_key[NETWORK]["bullets"] == ["장비 2종"]
    assert by_key[NETWORK]["reflecting"] is False
    assert by_key[STORAGE]["headline"] == "용량 증설 문의"
    assert by_key[STORAGE]["bullets"] == []
    assert by_key[STORAGE]["reflecting"] is True


def test_around_window_and_due_label(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread(
                "far",
                kind="요청",
                direction="받음",
                tags=[NETWORK],
                field="deadline",
                value="2026-11-27",
                occurred_at=when("01"),
            ),
            thread(
                "visit",
                kind="일정",
                tags=[STORAGE],
                field="schedule",
                value={
                    "start": "2026-11-21T15:00:00+09:00",
                    "end": None,
                    "timezone": "Asia/Seoul",
                },
                occurred_at=when("01"),
            ),
            thread(
                "past",
                kind="요청",
                direction="받음",
                tags=[NETWORK],
                field="deadline",
                value="2026-11-10",
                occurred_at=when("01"),
            ),
            thread(
                "later",
                kind="요청",
                direction="받음",
                tags=[NETWORK],
                field="deadline",
                value="2026-11-28",
                occurred_at=when("01"),
            ),
            thread(
                "now",
                kind="요청",
                direction="받음",
                tags=[NETWORK],
                field="deadline",
                value="2026-11-20",
                occurred_at=when("01"),
            ),
        ]
    )
    items = cards(h, headers, "around")["items"]
    assert [c["due_label"] for c in items] == ["내일", "D-7"]
    assert [c["due_kind"] for c in items] == ["schedule", "deadline"]
    assert [c["day"] for c in items] == ["2026-11-21", "2026-11-27"]
    assert items[1]["thread_ids"] == ["far"]


def write_document(h, page_key, bullets, title):
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    path = f"pages/{page_key}.md"
    store.write(path, {"kind": "area"}, "# 문서\n", None)
    store.write_metadata(
        path,
        {"bullets": bullets, "page_key": page_key, "title": title},
    )
    return store.page_id(path), store.page_id("Digest/2026/11/2026-11-20.md")


def test_card_link_document_else_digest(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread(
                "t1",
                kind="요청",
                direction="받음",
                tags=[NETWORK],
                occurred_at=when("20"),
            ),
            thread(
                "t2",
                kind="요청",
                direction="받음",
                tags=[STORAGE],
                occurred_at=when("20", 10),
            ),
        ]
    )
    document_id, digest_id = write_document(
        h,
        NETWORK,
        [{"bullet_id": "b1", "section": "current", "thread_ids": ["t1"]}],
        "네트워크",
    )
    by_key = {c["key"]: c for c in cards(h, headers)["items"]}
    link = by_key[NETWORK]["link"]
    assert link == {
        "kind": "document",
        "document_id": document_id,
        "bullet_id": "b1",
        "block_id": None,
    }
    other = by_key[STORAGE]["link"]
    assert other["kind"] == "digest" and other["document_id"] == digest_id
    assert other["bullet_id"] is None and other["block_id"] == block_id("t2")


def test_tasks_only_received_open(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread(
                "mine",
                kind="요청",
                direction="받음",
                content="견적 회신",
                counterpart="김철수",
                tags=[NETWORK],
                field="deadline",
                value="2026-11-25",
                occurred_at=when("19"),
            ),
            thread(
                "sent",
                kind="요청",
                direction="보냄",
                content="자료 요청",
                counterpart="이영희",
                tags=[NETWORK],
                occurred_at=when("19"),
            ),
            thread(
                "done",
                kind="요청",
                direction="받음",
                content="이미 끝난 일",
                tags=[NETWORK],
                occurred_at=when("18"),
            ),
            thread(
                "fin",
                kind="결정",
                resolves="done",
                tags=[NETWORK],
                occurred_at=when("19", 10),
            ),
        ]
    )
    document_id, _ = write_document(h, NETWORK, [], "네트워크")
    response = h.client.get(f"{READ}/tasks", headers=headers, params={"at": NOON})
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    [task] = body["items"]
    revision = {
        t["thread_id"]: t["revision"]
        for t in h.client.get(f"{READ}/threads", headers=headers).json()["items"]
    }
    assert task["thread_id"] == "mine" and task["revision"] == revision["mine"]
    assert task["deadline"] == "2026-11-25" and task["due_label"] == "D-5"
    assert task["counterpart"] == "김철수"
    assert task["document_id"] == document_id and task["document_title"] == "네트워크"
    assert task["link"] == {
        "kind": "document",
        "document_id": document_id,
        "bullet_id": None,
        "block_id": block_id("mine"),
    }


def test_results_validate_against_schema(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread(
                "t1",
                kind="요청",
                direction="받음",
                tags=[NETWORK],
                counterpart="김철수",
                occurred_at=when("20"),
            ),
            thread("u1", occurred_at=when("20", 10)),
        ]
    )
    write_document(h, NETWORK, [], "네트워크")
    for model, items in (
        (ResultCard, cards(h, headers)["items"]),
        (
            TaskItem,
            h.client.get(f"{READ}/tasks", headers=headers, params={"at": NOON}).json()[
                "items"
            ],
        ),
    ):
        assert items
        schema = model.model_json_schema(mode="validation", by_alias=True)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for item in items:
            validator.validate(item)
            model.model_validate(item)


def test_results_flag_and_bad_instant(tmp_path):
    h, headers = setup(tmp_path)
    features = h.client.get("/mobile/v1/capabilities", headers=headers).json()[
        "features"
    ]
    assert features["results"] is True
    bad = h.client.get(f"{READ}/cards", headers=headers, params={"at": "2026-11-20"})
    assert bad.status_code == 422


# ---- Meta alias follow-ups from the cards review ------------------------------------


def snapshot_files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_read_endpoints_create_no_files(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread(
                "t1",
                kind="요청",
                direction="받음",
                tags=[NETWORK],
                occurred_at=when("20"),
            )
        ]
    )
    root = h.config.state_root
    assert not (root / "wiki-digest.sqlite3").exists()
    assert not (root / "wiki.git").exists()
    # The first request may build lazily created stores shared with other routes
    # (Fabric, dictionary); only the wiki and compile state must stay absent.
    h.client.get(f"{READ}/threads", headers=headers)
    before = snapshot_files(root)
    calls = [
        ("cards", {"at": NOON}),
        ("cards", {"at": NOON, "window": "around"}),
        ("tasks", {"at": NOON}),
        ("tree", {"at": NOON}),
        ("documents", {}),
        ("sources", {}),
        ("references", {"path": NETWORK}),
        ("documents/missing", {}),
        ("documents/missing/bullets/b1/evidence", {}),
    ]
    for route, params in calls:
        response = h.client.get(f"{READ}/{route}", headers=headers, params=params)
        assert response.status_code in (200, 404), (route, response.text)
    assert snapshot_files(root) == before
    assert not (root / "wiki-digest.sqlite3").exists()
    assert not (root / "wiki-documents.sqlite3").exists()
    assert not (root / "wiki.git").exists()


def test_zone_boundary_is_the_next_day_in_seoul(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            thread("early", tags=[NETWORK], occurred_at=when("20", 0)),
            thread("late", tags=[STORAGE], occurred_at=when("19", 23)),
        ]
    )
    # 2026-11-19T16:00Z is 2026-11-20 01:00 in Seoul, one day ahead of UTC.
    body = cards(h, headers, at="2026-11-19T16:00:00Z")
    assert body["today"] == "2026-11-20"
    assert [c["key"] for c in body["items"]] == [NETWORK]
    before = cards(h, headers, at="2026-11-19T14:59:00Z")
    assert before["today"] == "2026-11-19"
    assert [c["key"] for c in before["items"]] == [STORAGE]


# ---- result read model 2: tree, documents, evidence, source log ---------------------

SEOUL = ZoneInfo("Asia/Seoul")
DAY = date(2026, 11, 20)
DAILY = "Digest/2026/11/2026-11-20.md"


def doc_store(h):
    return WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )


def put_document(
    h,
    key,
    title,
    bullets,
    rows=(),
    cautions=(),
    reformat=None,
    caution_day=date(2026, 11, 19),
    as_of=DAY,
):
    """Write one compiled document the way DocumentCompiler does, without a model."""
    store = doc_store(h)
    body = layout.render_document(
        title,
        as_of,
        [
            layout.Bullet(f"b{n}", text, tuple(ids))
            for n, (text, ids) in enumerate(bullets, 1)
        ],
        layout.Remaining(mine=list(rows), waiting=[]),
        [
            (
                layout.Caution(cid, label, caution_day, tuple(ids)),
                (reformat or {}).get(text, text),
            )
            for cid, label, text, ids in cautions
        ],
        [],
        SEOUL,
    )
    path = f"pages/{key}.md"
    store.write(
        path,
        {},
        body,
        None,
        readonly=True,
    )
    store.write_metadata(
        path,
        {
            "bullets": [
                {"bullet_id": f"b{n}", "section": "current", "thread_ids": ids}
                for n, (_, ids) in enumerate(bullets, 1)
            ],
            "cautions": [
                {
                    "candidate_id": cid,
                    "date": caution_day.isoformat(),
                    "label": label,
                    "text": text,
                    "thread_ids": ids,
                }
                for cid, label, text, ids in cautions
            ],
            "page_key": key,
            "title": title,
            "kind": node_kind(key),
            "as_of": as_of.isoformat(),
            "source_ids": [],
        },
    )
    return store.page_id(path)


def put_digest(h, lines, summary="- 네트워크 견적 도착 [[summaries/견적 src_x|견적]]"):
    store = doc_store(h)
    body = (
        "# 2026-11-20 (금)\n\n## 들어온 일\n\n"
        + "\n".join(lines)
        + "\n\n## 요약\n\n"
        + summary
        + "\n"
    )
    store.write(DAILY, {"source_ids": []}, body, None, readonly=True)
    return store.page_id(DAILY)


def task_row(tid, content, counterpart=None, mine=True, key=NETWORK):
    return layout.TaskRow(
        thread_id=tid,
        revision=1,
        content=content,
        block=block_id(tid),
        deadline=None,
        due_label=None,
        counterpart=counterpart,
        mine=mine,
        page_key=key,
    )


def get(h, headers, route, **params):
    return h.client.get(f"{READ}/{route}", headers=headers, params=params)


def request_thread(tid, content="견적 회신", tags=(NETWORK,), at=None, **kw):
    return thread(
        tid,
        kind="요청",
        direction="받음",
        content=content,
        counterpart="김철수",
        tags=list(tags),
        occurred_at=at or when("20"),
        **kw,
    )


def test_tree_lists_nodes_with_documents(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            request_thread("t1"),
            thread("old", tags=[STORAGE], occurred_at=when("01")),
        ]
    )
    document_id = put_document(h, NETWORK, "네트워크", [("현황", ["t1"])])
    body = get(h, headers, "tree", at=NOON).json()
    nodes = {n["path"]: n for n in body["items"]}
    assert {"회사", "회사/infra", NETWORK, STORAGE} <= nodes.keys()
    assert nodes[NETWORK]["document_id"] == document_id
    assert nodes["회사/infra"]["document_id"] is None
    assert nodes[NETWORK]["parent"] == "회사/infra" and nodes["회사"]["parent"] is None
    assert nodes[NETWORK]["kind"] == "topic" or nodes[NETWORK]["kind"] == "area"
    # Counts roll up to ancestors; the 11/01 note is outside the seven-day window.
    assert nodes[NETWORK]["open_tasks"] == 1 and nodes["회사"]["open_tasks"] == 1
    assert nodes[NETWORK]["cards_7d"] == 1 and nodes["회사/infra"]["cards_7d"] == 1
    assert nodes[STORAGE]["cards_7d"] == 0
    assert "회사/미분류" not in nodes


def test_document_sections_live_remaining(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            request_thread("t1"),
            thread(
                "t2",
                kind="요청",
                direction="보냄",
                content="자료 요청",
                counterpart="이영희",
                tags=[NETWORK],
                occurred_at=when("20", 10),
            ),
        ]
    )
    document_id = put_document(
        h,
        NETWORK,
        "네트워크",
        [("견적서 수령", ["t1"])],
        rows=[task_row("t1", "견적 회신")],
        cautions=[("c-1", "기한 변경", "기한이 미뤄짐", ["t1"])],
    )
    view = get(h, headers, f"documents/{document_id}", at=NOON).json()
    sections = {s["kind"]: s for s in view["sections"]}
    assert list(sections) == ["current", "remaining", "waiting", "cautions"]
    [task] = sections["remaining"]["bullets"]
    assert task["task"]["thread_id"] == "t1" and task["bullet_id"] == block_id("t1")
    assert task["task"]["link"]["document_id"] == document_id
    [wait] = sections["waiting"]["bullets"]
    assert wait["text"] == "자료 요청 · 이영희" and wait["task"] is None
    [caution] = sections["cautions"]["bullets"]
    assert caution["label"] == "기한 변경" and caution["day"] == "2026-11-19"
    assert caution["bullet_id"] == "c-1" and caution["thread_ids"] == ["t1"]
    assert view["title"] == "네트워크" and view["as_of"] == "2026-11-20"
    assert view["log_subject"] == NETWORK and view["kind"] in ("area", "topic")
    current = sections["current"]["bullets"]

    # Resolving the request only appends to Fabric; the document file is not rewritten.
    h.fabric.append(
        [
            thread(
                "done",
                kind="결정",
                resolves="t1",
                tags=[NETWORK],
                occurred_at=when("20", 11),
            )
        ]
    )
    after = get(h, headers, f"documents/{document_id}", at=NOON).json()
    kinds = [s["kind"] for s in after["sections"]]
    assert "remaining" not in kinds and "waiting" in kinds
    assert {s["kind"]: s for s in after["sections"]}["current"]["bullets"] == current
    assert after["sha256"] == view["sha256"]
    assert after["reflecting"] is True


def test_digest_document_tasks(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            request_thread("t1", at=when("20")),
            request_thread("t2", content="일정 확정", at=when("20", 10)),
        ]
    )
    lines = [
        layout.task_line("견적 회신", block_id("t1"), deadline=None, done=None),
        layout.task_line(
            "일정 확정", block_id("t2"), deadline=date(2026, 11, 22), done=DAY
        ),
    ]
    document_id = put_digest(h, lines)
    view = get(h, headers, f"documents/{document_id}", at=NOON).json()
    assert view["kind"] == "digest" and view["key"] == "2026-11-20"
    assert view["as_of"] == "2026-11-20" and view["log_subject"] is None
    assert view["title"] == "2026-11-20 (금)"
    sections = {s["kind"]: s for s in view["sections"]}
    assert list(sections) == ["tasks", "summary"]
    tasks = sections["tasks"]["bullets"]
    assert [b["task"]["block_id"] for b in tasks] == [block_id("t1"), block_id("t2")]
    assert [b["task"]["thread_id"] for b in tasks] == ["t1", "t2"]
    assert tasks[1]["label"] == "완료" and tasks[1]["day"] == "2026-11-22"
    assert tasks[0]["task"]["link"] == {
        "kind": "digest",
        "document_id": document_id,
        "bullet_id": None,
        "block_id": block_id("t1"),
    }
    [summary] = sections["summary"]["bullets"]
    assert summary["text"] == "네트워크 견적 도착"
    listed = {d["document_id"]: d for d in get(h, headers, "documents").json()["items"]}
    assert listed[document_id]["kind"] == "digest"


FULL_SCRIPT = "통화 전문: 견적서는 금요일까지 보내 주세요.\n" * 3


def put_summary(h, source_id, script, digest="a" * 64):
    folder = h.config.archive_root / "summaries" / digest[:2]
    folder.mkdir(parents=True)
    summary = EventSummary.model_validate(
        {
            "schema_version": 2,
            "summary_id": digest,
            "source_id": source_id,
            "script": script,
            "body": "요약",
            "replaces": None,
            "created_at": NOW.isoformat(),
        }
    )
    (folder / f"{digest}.json").write_text(summary.model_dump_json())


def test_evidence_bullet_to_threads_to_whole_source(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            request_thread("t1"),
            thread("t2", tags=[NETWORK], occurred_at=when("20", 10), source_id=SRC_B),
            thread("t3", tags=[NETWORK], occurred_at=when("20", 11)),
        ]
    )
    put_summary(h, SID, FULL_SCRIPT)
    document_id = put_document(
        h, NETWORK, "네트워크", [("현황", ["t2", "t1"]), ("다른 현황", ["t3"])]
    )
    response = get(h, headers, f"documents/{document_id}/bullets/b1/evidence")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "현황" and body["bullet_id"] == "b1"
    assert [t["thread_id"] for t in body["threads"]] == ["t2", "t1"]
    assert [s["source_id"] for s in body["sources"]] == [SRC_B, SID]
    by_id = {s["source_id"]: s for s in body["sources"]}
    assert by_id[SID]["text"] == FULL_SCRIPT and by_id[SID]["truncated"] is False
    # A source with no archived script is a person's record: its Thread contents.
    assert by_id[SRC_B]["text"] == "t2"
    Evidence.model_validate(body)

    put_summary(h, SRC_B, "가" * 25000, digest="b" * 64)
    long = get(h, headers, f"documents/{document_id}/bullets/b1/evidence").json()
    cut = {s["source_id"]: s for s in long["sources"]}[SRC_B]
    assert len(cut["text"]) == 20000 and cut["truncated"] is True


def test_evidence_unknown_bullet_404(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append([request_thread("t1")])
    document_id = put_document(h, NETWORK, "네트워크", [("현황", ["t1"])])
    missing = get(h, headers, f"documents/{document_id}/bullets/b9/evidence")
    assert missing.status_code == 404 and missing.json()["code"] == "bullet_not_found"
    nothing = get(h, headers, "documents/nope/bullets/b1/evidence")
    assert nothing.status_code == 404
    assert nothing.json()["code"] == "document_not_found"
    assert get(h, headers, "documents/nope").status_code == 404


def seed_sources(h):
    ContextDictionary(h.config.state_root).sync_tree(
        "가족", [TreeNode(n, node_kind(n)) for n in ("가족", "가족/집안일")]
    )
    src_c = "src_01ARZ3NDEKTSV4RRFFQ69G5FAX"
    h.fabric.append(
        [
            request_thread("a1", at=when("20", 9)),
            thread("a2", tags=[NETWORK], occurred_at=when("20", 9)),
            thread(
                "a3",
                tags=[STORAGE],
                counterpart="이영희",
                occurred_at=when("20", 9),
            ),
            schedule(
                "b1",
                tags=("가족/집안일",),
                counterpart="홍길동",
                source_id=SRC_B,
                occurred_at=when("21"),
            ),
            thread(
                "c1",
                tags=[NETWORK],
                counterpart="홍길동",
                source_id=src_c,
                occurred_at=when("19"),
            ),
        ]
    )
    return src_c


def test_sources_grouped_one_row_per_source_with_used_in(tmp_path):
    h, headers = setup(tmp_path)
    seed_sources(h)
    document_id = put_document(h, NETWORK, "네트워크", [("현황", ["a2"])])
    digest_id = put_digest(
        h, [layout.task_line("견적 회신", block_id("a1"), deadline=None, done=None)]
    )
    body = get(h, headers, "sources").json()
    assert body["next_cursor"] is None
    first = next(e for e in body["items"] if e["source_id"] == SID)
    assert [t["thread_id"] for t in first["threads"]] == ["a1", "a2", "a3"]
    assert sorted(first["paths"]) == [NETWORK, STORAGE]
    assert first["spheres"] == ["회사"]
    assert first["people"] == ["김철수", "이영희"]
    assert first["occurred_at"].startswith("2026-11-20T00:00:00")
    assert first["used_in"] == [
        {
            "kind": "document",
            "document_id": document_id,
            "title": "네트워크",
            "bullet_id": "b1",
            "block_id": None,
        },
        {
            "kind": "digest",
            "document_id": digest_id,
            "title": "2026-11-20 (금)",
            "bullet_id": None,
            "block_id": block_id("a1"),
        },
    ]
    # Newest source first.
    assert [e["source_id"] for e in body["items"]][0] == SRC_B


def test_sources_filters_and_paging(tmp_path):
    h, headers = setup(tmp_path)
    src_c = seed_sources(h)

    def ids(**params):
        response = get(h, headers, "sources", **params)
        assert response.status_code == 200, response.text
        return [e["source_id"] for e in response.json()["items"]]

    assert ids(sphere="가족") == [SRC_B]
    assert ids(person="홍길동") == [SRC_B, src_c]
    assert ids(person="김철수") == [SID]
    assert ids(kind="일정") == [SRC_B]
    assert ids(kind="요청") == [SID]
    assert ids(sphere="회사", kind="일정") == []
    first = get(h, headers, "sources", limit=2).json()
    assert [e["source_id"] for e in first["items"]] == [SRC_B, SID]
    assert first["next_cursor"] == "o2"
    rest = get(h, headers, "sources", limit=2, cursor="o2").json()
    assert [e["source_id"] for e in rest["items"]] == [src_c]
    assert rest["next_cursor"] is None
    bad = get(h, headers, "sources", cursor="x1")
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_cursor"


def test_document_etag_304(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append([request_thread("t1")])
    document_id = put_document(h, NETWORK, "네트워크", [("현황", ["t1"])])
    first = get(h, headers, f"documents/{document_id}", at=NOON)
    tag = first.headers["etag"]
    again = h.client.get(
        f"{READ}/documents/{document_id}",
        headers={**headers, "If-None-Match": tag},
        params={"at": NOON},
    )
    assert again.status_code == 304 and again.headers["etag"] == tag
    # The live remaining section changed, so the tag must too.
    h.fabric.append(
        [
            thread(
                "done",
                kind="결정",
                resolves="t1",
                tags=[NETWORK],
                occurred_at=when("20", 11),
            )
        ]
    )
    changed = h.client.get(
        f"{READ}/documents/{document_id}",
        headers={**headers, "If-None-Match": tag},
        params={"at": NOON},
    )
    assert changed.status_code == 200 and changed.headers["etag"] != tag


def test_references_route_without_index_is_empty(tmp_path):
    h, headers = setup(tmp_path)
    response = get(h, headers, "references", path=NETWORK)
    assert response.status_code == 200 and response.json() == {"items": []}


def test_result_models_validate_against_schema(tmp_path):
    h, headers = setup(tmp_path)
    seed_sources(h)
    put_summary(h, SID, FULL_SCRIPT)
    document_id = put_document(
        h,
        NETWORK,
        "네트워크",
        [("현황", ["a2"])],
        rows=[task_row("a1", "견적 회신")],
        cautions=[("c-1", "번복", "내용 번복", ["a2"])],
    )
    digest_id = put_digest(
        h, [layout.task_line("견적 회신", block_id("a1"), deadline=None, done=None)]
    )
    checks = (
        (TreeNodeView, get(h, headers, "tree", at=NOON).json()["items"]),
        (DocumentSummary, get(h, headers, "documents").json()["items"]),
        (
            DocumentView,
            [
                get(h, headers, f"documents/{i}", at=NOON).json()
                for i in (document_id, digest_id)
            ],
        ),
        (
            Evidence,
            [get(h, headers, f"documents/{document_id}/bullets/b1/evidence").json()],
        ),
        (SourceLogEntry, get(h, headers, "sources").json()["items"]),
    )
    for model, items in checks:
        assert items, model
        schema = model.model_json_schema(mode="validation", by_alias=True)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for item in items:
            validator.validate(item)
            model.model_validate(item)


def retract(tid, target, hour=12):
    return thread(
        tid, kind="기록", retracts=target, tags=[NETWORK], occurred_at=when("20", hour)
    )


def test_evidence_drops_retracted_threads_and_their_source_text(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append(
        [
            request_thread("t1"),
            thread("t2", tags=[NETWORK], occurred_at=when("20", 10), source_id=SRC_B),
            retract("r1", "t2"),
        ]
    )
    put_summary(h, SID, FULL_SCRIPT)
    put_summary(h, SRC_B, "철회된 원본 전문", digest="b" * 64)
    document_id = put_document(
        h, NETWORK, "네트워크", [("현황", ["t1", "t2"]), ("철회만", ["t2"])]
    )
    body = get(h, headers, f"documents/{document_id}/bullets/b1/evidence").json()
    assert [t["thread_id"] for t in body["threads"]] == ["t1"]
    assert [s["source_id"] for s in body["sources"]] == [SID]
    only = get(h, headers, f"documents/{document_id}/bullets/b2/evidence").json()
    assert only["threads"] == [] and only["sources"] == []
    assert "철회된 원본 전문" not in str(only)


def test_unmatched_caution_line_does_not_shift_later_ones(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append([request_thread("t1"), request_thread("t2", content="다른 일")])
    document_id = put_document(
        h,
        NETWORK,
        "네트워크",
        [("현황", ["t1"])],
        cautions=[
            ("c-1", "기한 변경", "첫째 주의", ["t1"]),
            ("c-2", "번복", "둘째 주의", ["t2"]),
        ],
        reformat={"첫째 주의": "첫째 주의 (손질)"},
    )
    view = get(h, headers, f"documents/{document_id}", at=NOON).json()
    cautions = {s["kind"]: s for s in view["sections"]}["cautions"]["bullets"]
    assert [(c["bullet_id"], c["thread_ids"]) for c in cautions] == [
        ("c1", []),
        ("c-2", ["t2"]),
    ]


def test_december_caution_on_january_page_gets_previous_year(tmp_path):
    h, headers = setup(tmp_path)
    h.fabric.append([request_thread("t1")])
    document_id = put_document(
        h,
        NETWORK,
        "네트워크",
        [("현황", ["t1"])],
        cautions=[("c-1", "번복", "연말 번복", ["t1"])],
        caution_day=date(2025, 12, 30),
        as_of=date(2026, 1, 5),
    )
    view = get(h, headers, f"documents/{document_id}", at=NOON).json()
    [caution] = {s["kind"]: s for s in view["sections"]}["cautions"]["bullets"]
    assert caution["day"] == "2025-12-30"


def test_sources_cursor_rejects_non_ascii_digits(tmp_path):
    h, headers = setup(tmp_path)
    for cursor in ("o²", "o-1", "o"):
        response = get(h, headers, "sources", cursor=cursor)
        assert response.status_code == 422, cursor


def feedback_headers(h):
    return h.pair("read", "edit", label="feedback")


def test_feedback_endpoints_journaled(tmp_path):
    h, headers = setup(tmp_path)
    headers = feedback_headers(h)
    h.fabric.append([request_thread("t1")])
    document_id = put_document(h, NETWORK, "네트워크", [("현황", ["t1"])])
    body = {
        "client_id": "feedback-0001",
        "document_id": document_id,
        "bullet_id": "b1",
        "text": "견적 회신은 김철수가 아니라 홍길동 담당",
        "chips": ["사실이 틀림"],
    }
    first = h.client.post(f"{BASE}/edits/feedback", headers=headers, json=body)
    assert first.status_code == 200, first.text
    item = first.json()["result"]
    assert item["status"] == "pending" and item["classification"] is None
    assert "Idempotent-Replay" not in first.headers
    again = h.client.post(f"{BASE}/edits/feedback", headers=headers, json=body)
    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json()["result"]["feedback_id"] == item["feedback_id"]
    listed = h.client.get(f"{READ}/documents/{document_id}/feedback", headers=headers)
    assert listed.status_code == 200
    assert [i["feedback_id"] for i in listed.json()["items"]] == [item["feedback_id"]]
    assert listed.json()["next_cursor"] is None
    unknown = h.client.post(
        f"{BASE}/edits/feedback",
        headers=headers,
        json={**body, "client_id": "feedback-0002", "document_id": "missing"},
    )
    assert unknown.status_code == 404 and unknown.json()["code"] == "document_not_found"
    undo = {"client_id": "feedback-undo-1", "feedback_id": item["feedback_id"]}
    done = h.client.post(f"{BASE}/edits/feedback/undo", headers=headers, json=undo)
    assert done.status_code == 200 and done.json()["result"]["status"] == "undone"
    second = h.client.post(
        f"{BASE}/edits/feedback/undo",
        headers=headers,
        json={**undo, "client_id": "feedback-undo-2"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "feedback_already_undone"
    features = h.client.get(f"{BASE}/capabilities", headers=headers).json()
    assert features["features"]["feedback"] is True
