import re
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from weaver.context import ContextEntry, DictionarySnapshot
from weaver.schema import Thread
from weaver.wiki.layout import (
    Bullet,
    Caution,
    RefLink,
    Remaining,
    TaskRow,
    block_id,
    case_key,
    caution_candidates,
    due_label,
    open_tasks,
    page_key,
    parse_task_marks,
    remaining,
    render_document,
    task_line,
)

SRC_A = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
SRC_B = "src_01ARZ3NDEKTSV4RRFFQ69G5FAW"
ZONE = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 20, 3, tzinfo=UTC)
PATHS = [
    ("회사", "area"),
    ("회사/infra", "topic"),
    ("회사/infra/network", "area"),
    ("회사/infra/network/router", "topic"),
    ("회사/project", "topic"),
    ("회사/project/26-12-라우터-교체", "project"),
    ("회사/stacks", "topic"),
    ("회사/stacks/fortigate", "topic"),
    ("회사/미분류", "area"),
]
DICT = DictionarySnapshot([ContextEntry(n, k, source="tree") for n, k in PATHS])


def thread(tid, tags=(), seq=1, kind="기록", source=SRC_A, **kw):
    base = {
        "thread_id": tid,
        "kind": kind,
        "content": "내용" if kind != "거절" else None,
        "occurred_at": NOW,
        "recorded_at": NOW,
        "tags": list(tags),
        "field": None,
        "value": None,
        "source_id": source,
        "actor": "ai_approved",
        "direction": "받음" if kind == "요청" else None,
        "resolves": None,
        "retracts": None,
        "correction": None,
        "seq": seq,
    }
    return Thread(**{**base, **kw})


def test_block_id_stable_and_safe():
    b = block_id("phone-request:abc")
    assert re.fullmatch(r"w-[0-9a-f]{10}", b)
    assert b == block_id("phone-request:abc")


def test_task_line_roundtrip():
    b = block_id("t1")
    line = task_line("라우터 교체 견적 회신", b, deadline=date(2026, 11, 30), done=None)
    assert line == "- [ ] 라우터 교체 견적 회신 📅 2026-11-30 ^" + b
    assert parse_task_marks(line)[b].checked is False
    done = task_line("회신\n완료", b, deadline=None, done=date(2026, 11, 21))
    assert done == f"- [x] 회신 완료 ✅ 2026-11-21 ^{b}"
    mark = parse_task_marks(done)[b]
    assert mark.checked and mark.done == date(2026, 11, 21)


def test_parse_marks_monotonic():
    b = block_id("t1")
    text = f"- [ ] a ^{b}\n- [x] a ^{b}\n"
    assert parse_task_marks(text)[b].checked is True


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (
            ["회사/infra/network/router", "회사/project/26-12-라우터-교체"],
            "회사/project/26-12-라우터-교체",
        ),
        (["회사/infra/network/router"], "회사/infra/network"),
        (["회사/stacks/fortigate"], "회사/stacks/fortigate"),
        (["회사/미분류"], None),
        (["아무태그"], None),
    ],
)
def test_page_key_rules(tags, expected):
    assert page_key(thread("t", tags), DICT) == expected


def test_case_key_merges_same_target_not_unclassified():
    a = thread("a", ["회사/infra/network/router"], source=SRC_A)
    b = thread("b", ["회사/infra/network/router"], source=SRC_B)
    assert case_key(a, DICT) == case_key(b, DICT) == "회사/infra/network/router"
    u1 = thread("c", ["회사/미분류"], source=SRC_A)
    u2 = thread("d", ["회사/미분류"], source=SRC_B)
    assert case_key(u1, DICT) != case_key(u2, DICT)
    p = thread("e", ["회사/infra/network/router", "회사/project/26-12-라우터-교체"])
    assert case_key(p, DICT) == "회사/project/26-12-라우터-교체"


@pytest.mark.parametrize(
    ("day", "label"),
    [(20, "오늘"), (21, "내일"), (19, "어제"), (30, "D-10"), (18, "2일 전")],
)
def test_due_label(day, label):
    assert due_label(date(2026, 11, day), date(2026, 11, 20)) == label


def mobile(tid, seq, direction, deadline=None, **kw):
    base = {
        "thread_id": tid,
        "seq": seq,
        "revision": 1,
        "open": True,
        "retracted": False,
        "presentation": "fact",
        "effective_deadline": deadline,
        "content": f"할 일 {tid}",
        "counterpart": "김철수",
        "direction": direction,
        "tags": ["회사/project/26-12-라우터-교체"],
        "source_id": SRC_A,
    }
    return SimpleNamespace(**{**base, **kw})


def test_open_tasks_mine_and_order():
    rows = [
        mobile("none", 1, "받음"),
        mobile("late", 2, "받음", "2026-11-30"),
        mobile("wait", 3, "보냄"),
        mobile("resolved", 4, "받음", open=False),
        mobile("gone", 5, "받음", retracted=True),
    ]
    tasks = open_tasks(rows, DICT, date(2026, 11, 20))
    assert [t.thread_id for t in tasks] == ["late", "none", "wait"]
    assert tasks[0].due_label == "D-10"
    rest = remaining("회사/project/26-12-라우터-교체", tasks)
    assert [t.thread_id for t in rest.mine] == ["late", "none"]
    assert [t.thread_id for t in rest.waiting] == ["wait"]


def test_caution_candidates_only_judgment_changing():
    base = thread("base", ["회사/infra/network/router"], kind="요청")
    amend = thread(
        "amend",
        ["회사/infra/network/router"],
        seq=2,
        kind="결정",
        actor="human",
        field="deadline",
        value="2026-12-05",
        amends="base",
    )
    plain = thread("plain", ["회사/infra/network/router"], seq=3)
    undo = thread(
        "phone-request:u2",
        [],
        seq=4,
        kind="결정",
        actor="human",
        retracts="phone-request:u1",
    )
    undo_target = thread(
        "phone-request:u1", [], seq=5, kind="결정", actor="human", resolves="base"
    )
    feedback = thread(
        "feedback:x", [], seq=6, kind="결정", actor="system", retracts="base"
    )
    got = caution_candidates(
        [base, undo_target], [base, amend, plain, undo, undo_target, feedback], ZONE
    )
    assert [c.label for c in got] == ["기한 변경"]
    assert re.fullmatch(r"c-[0-9a-f]{8}", got[0].candidate_id)
    assert got[0].day == date(2026, 11, 20)


def test_render_hides_empty_sections():
    text = render_document(
        "라우터 교체", date(2026, 11, 20), [], Remaining([], []), [], [], ZONE
    )
    for heading in ("## 주의할 이력", "## 관련 자료", "## 남은 일", "## 기다리는 중"):
        assert heading not in text
    assert "_11월 20일 기준_" in text
    assert text.startswith("# 라우터 교체\n")


def test_task_line_roundtrip_with_deadline_and_done():
    b = block_id("t1")
    line = task_line("회신", b, deadline=date(2026, 11, 30), done=date(2026, 11, 21))
    assert line == f"- [x] 회신 📅 2026-11-30 ✅ 2026-11-21 ^{b}"
    mark = parse_task_marks(line)[b]
    assert mark.checked and mark.done == date(2026, 11, 21)


def test_tasks_plugin_style_tick_parses():
    b = block_id("t1")
    line = f"- [x] 회신 📅 2026-11-30 ✅ 2026-11-22 ^{b}"
    mark = parse_task_marks(line)[b]
    assert mark.checked is True and mark.done == date(2026, 11, 22)
    plain = f"- [x] 회신 ✅ 2026-11-22 ^{b}"
    assert parse_task_marks(plain)[b].checked is True


def test_caution_day_uses_zone():
    late = datetime(2026, 11, 20, 16, tzinfo=UTC)
    base = thread("base", ["회사/infra/network/router"], kind="요청")
    amend = thread(
        "amend",
        [],
        seq=2,
        kind="결정",
        actor="human",
        field="deadline",
        value="2026-12-05",
        amends="base",
        recorded_at=late,
    )
    got = caution_candidates([base], [amend], ZONE)
    assert got[0].day == date(2026, 11, 21)


def _row(tid, mine, counterpart, deadline=None):
    return TaskRow(
        tid, 1, f"할 일 {tid}", block_id(tid), deadline, None, counterpart, mine, "k"
    )


def test_render_full_document_roundtrips_blocks():
    mine = [_row("m1", True, "김철수", date(2026, 11, 30)), _row("m2", True, None)]
    waiting = [_row("w1", False, "홍길동"), _row("w2", False, None)]
    cautions = [
        (
            Caution("c-12345678", "기한 변경", date(2026, 11, 21), ("a", "b")),
            "마감 연기",
        )
    ]
    refs = [
        RefLink("개인", "프로젝트/라우터", "견적", True),
        RefLink("회사", "docs/network.md", "구성", False),
    ]
    text = render_document(
        "라우터 교체",
        date(2026, 11, 20),
        [Bullet("b1", "견적 대기", ("a",))],
        Remaining(mine, waiting),
        cautions,
        refs,
        ZONE,
    )
    order = [
        text.index(h)
        for h in (
            "## 현재 상태",
            "## 남은 일",
            "## 기다리는 중",
            "## 주의할 이력",
            "## 관련 자료",
        )
    ]
    assert order == sorted(order)
    assert "- 견적 대기" in text
    assert "- **11/21** [기한 변경] 마감 연기" in text
    assert "- [[프로젝트/라우터|견적]]" in text
    assert "- `회사 vault` docs/network.md › 구성" in text
    assert "할 일 w1 · 홍길동 ^" in text
    marks = parse_task_marks(text)
    assert set(marks) == {block_id(t) for t in ("m1", "m2", "w1", "w2")}


def test_impossible_done_date_is_ticked_without_date():
    from weaver.wiki.layout import parse_task_marks

    marks = parse_task_marks("- [x] 일 ✅ 2026-02-30 ^w-0123456789\n")
    assert marks["w-0123456789"].checked and marks["w-0123456789"].done is None
