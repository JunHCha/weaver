"""The phone read model must agree with the web's fact/history, mention and
effective-value rules; every dataset here is synthetic."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from weaver.context import ContextDictionary
from weaver.mobile.api import utc_z
from weaver.mobile.read import ReadModel
from weaver.web import create_app
from weaver.wiki.store import WikiStore

from .helpers import Harness, thread

READ = "/mobile/v1/read"
ZONE = "+09:00"


def at(day: str, hour: int = 9) -> datetime:
    return datetime.fromisoformat(f"2026-11-{day}T{hour:02d}:00:00{ZONE}")


def seed(h: Harness) -> None:
    dictionary = ContextDictionary(h.config.state_root)
    dictionary.add_entry("이서준", "person", ("서준",))
    dictionary.add_entry("한지우", "person")
    dictionary.add_entry("협력사 A", "organization")
    dictionary.add_entry("네트워크", "area")
    dictionary.add_entry("방화벽 교체", "project")
    h.fabric.append(
        [
            thread(
                "t1",
                kind="요청",
                direction="받음",
                content="견적 회신",
                counterpart="이서준",
                tags=["협력사 A", "네트워크"],
                field="deadline",
                value="2026-11-10",
                occurred_at=at("05"),
                recorded_at=at("05"),
            ),
            thread(
                "t2",
                kind="요청",
                direction="받음",
                content="프린터 배포",
                counterpart="한지우",
                tags=["네트워크"],
                field="deadline",
                value="2026-11-12",
                occurred_at=at("06"),
                recorded_at=at("06"),
            ),
            thread(
                "t3",
                content="이서준 동행 필요",
                counterpart="한지우",
                tags=["서준", "네트워크"],
                occurred_at=at("11"),
                recorded_at=at("11"),
            ),
            thread(
                "t4",
                kind="일정",
                content="점검",
                counterpart="한지우",
                tags=["네트워크"],
                field="schedule",
                value={
                    "start": "2026-11-12T15:00:00+09:00",
                    "end": None,
                    "timezone": "Asia/Seoul",
                },
                occurred_at=at("11"),
                recorded_at=at("11", 10),
            ),
            thread(
                "d1",
                kind="결정",
                content="견적 처리",
                resolves="t2",
                counterpart="한지우",
                tags=["네트워크"],
                occurred_at=at("11", 11),
                recorded_at=at("11", 11),
            ),
            thread(
                "c1",
                kind="태그정정",
                content="웹에서 태그 정정",
                tags=[],
                correction={"target": "t1", "add": ["방화벽 교체"], "remove": []},
                occurred_at=at("11", 12),
                recorded_at=at("11", 12),
            ),
            thread(
                "j1",
                kind="거절",
                content=None,
                retracts=None,
                tags=["네트워크"],
                counterpart="이서준",
                occurred_at=at("11", 13),
                recorded_at=at("11", 13),
            ),
            thread(
                "wiki-field:네트워크:status",
                content="status 변경",
                tags=["네트워크"],
                field="status",
                value="진행 중",
                occurred_at=at("11", 14),
                recorded_at=at("11", 14),
            ),
            thread(
                "t5",
                content="취소될 기록",
                counterpart="이서준",
                tags=["네트워크"],
                occurred_at=at("11", 15),
                recorded_at=at("11", 15),
            ),
            thread(
                "r5",
                content="t5 철회",
                retracts="t5",
                tags=["네트워크"],
                occurred_at=at("11", 16),
                recorded_at=at("11", 16),
            ),
        ]
    )


def web_client(h: Harness) -> TestClient:
    return TestClient(create_app(h.config), base_url="http://127.0.0.1")


def test_fact_history_split_matches_the_web(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("read")
    web = {t["thread_id"]: t for t in web_client(h).get("/api/threads").json()}
    page = h.client.get(f"{READ}/threads", headers=headers).json()
    mine = {t["thread_id"]: t for t in page["items"]}
    assert set(mine) == set(web)
    for key, row in web.items():
        assert mine[key]["presentation"] == row["presentation"], key
        assert mine[key]["tags"] == row["tags"], key  # effective, canonical tags
        assert mine[key]["counterpart"] == row["counterpart"], key
    assert {k for k, t in mine.items() if t["presentation"] == "history"} == {
        "c1",
        "j1",
        "wiki-field:네트워크:status",
        "r5",
    }
    assert "방화벽 교체" in mine["t1"]["tags"]  # the correction is folded in
    assert (
        mine["t3"]["counterpart"] == "한지우" and "이서준" in mine["t3"]["tags"]
    )  # alias resolved


def test_open_requests_follow_resolve_and_retraction_rules(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    rows = {t.thread_id: t for t in ReadModel(h.config).threads(None, 100).items}
    assert rows["t1"].open is True and rows["t2"].open is False  # d1 resolves t2
    h.fabric.append(
        [
            thread(
                "x1",
                content="결정 철회",
                retracts="d1",
                tags=["네트워크"],
                occurred_at=at("12"),
                recorded_at=at("12"),
            )
        ]
    )
    rows = {t.thread_id: t for t in ReadModel(h.config).threads(None, 100).items}
    assert rows["t2"].open is True  # the retracted resolve no longer closes it
    assert rows["t3"].open is False  # not a request


def test_tag_states_and_dictionary_match_the_web(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("read")
    web = web_client(h)
    assert (
        h.client.get(f"{READ}/tags", headers=headers).json()["items"]
        == web.get("/api/tags").json()
    )
    assert h.client.get(f"{READ}/states", headers=headers).json() == utc_z(
        web.get("/api/states").json()
    )
    one = h.client.get(
        f"{READ}/states", params={"tag": "네트워크"}, headers=headers
    ).json()
    assert one == utc_z(web.get("/api/states", params={"tag": "네트워크"}).json())
    assert one["네트워크"]["status"] == "진행 중"


def test_threads_are_paged_by_seq_with_a_resumable_cursor(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("read")
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = h.client.get(f"{READ}/threads", params=params, headers=headers).json()
        seen += [t["seq"] for t in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:  # the last page
            break
    assert seen == sorted(seen) and len(seen) == len(set(seen)) == 10
    assert page["head"] == 10
    empty = h.client.get(
        f"{READ}/threads", params={"cursor": "c10"}, headers=headers
    ).json()
    assert empty["items"] == [] and empty["next_cursor"] is None
    bad = h.client.get(f"{READ}/threads", params={"cursor": "x"}, headers=headers)
    assert bad.json()["code"] == "invalid_cursor"
    assert (
        h.client.get(
            f"{READ}/threads", params={"limit": 501}, headers=headers
        ).status_code
        == 422
    )
    one = h.client.get(f"{READ}/threads/t1", headers=headers).json()
    assert one["thread_id"] == "t1" and one["open"] is True
    assert (
        h.client.get(f"{READ}/threads/nope", headers=headers).json()["code"]
        == "thread_not_found"
    )


def test_now_view_overdue_today_and_recent_in_the_configured_zone(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    view = ReadModel(h.config).now(at("12", 8))
    assert view.today == "2026-11-12"
    assert [e.thread.thread_id for e in view.overdue] == ["t1"]  # due 11-10, still open
    today = [(e.thread.thread_id, e.due_kind) for e in view.due_today]
    assert today == [("t4", "schedule")]  # t2 is resolved; the schedule is today
    recent = [e.thread.thread_id for e in view.recent]
    assert "t3" in recent and "t4" not in recent and "t1" not in recent
    assert all(e.thread.presentation == "fact" for e in view.recent)
    assert "t5" not in recent  # retracted facts never show up
    later = ReadModel(h.config).now(at("13"))
    assert [e.thread.thread_id for e in later.overdue] == ["t1"]
    assert later.due_today == []
    # Just before midnight Seoul is still the 12th even though UTC is the 12th too.
    edge = ReadModel(h.config).now(datetime(2026, 11, 12, 14, 59, tzinfo=UTC))
    assert edge.today == "2026-11-12"
    assert (
        ReadModel(h.config).now(datetime(2026, 11, 12, 15, 1, tzinfo=UTC)).today
        == "2026-11-13"
    )


def test_now_recent_sorts_by_occurrence_then_seq_without_changing_filters(tmp_path):
    h = Harness(tmp_path)
    h.fabric.append(
        [
            thread("acknowledged", occurred_at=at("20"), recorded_at=at("18")),
            thread("newer", occurred_at=at("19"), recorded_at=at("19")),
            thread("tie", occurred_at=at("19"), recorded_at=at("19")),
            thread("late", occurred_at=at("06"), recorded_at=at("20")),
            thread("unseen-old", occurred_at=at("05"), recorded_at=at("18")),
        ]
    )
    model = ReadModel(h.config)
    initial = model.now(at("20"))
    assert [e.thread.thread_id for e in initial.recent] == ["tie", "newer", "late"]
    unseen = model.now(at("20"), after_seq=1, fabric_epoch=initial.fabric_epoch)
    assert [e.thread.thread_id for e in unseen.recent] == [
        "tie",
        "newer",
        "late",
        "unseen-old",
    ]


def test_now_recent_orders_and_filters_across_the_seoul_date_boundary(tmp_path):
    h = Harness(tmp_path)
    utc = lambda day, hour, minute=0: datetime(2026, 11, day, hour, minute, tzinfo=UTC)  # noqa: E731
    h.fabric.append(
        [
            # Same UTC day (19th) but 11/19 23:00 vs 11/20 01:00 in Seoul.
            thread("before-midnight", occurred_at=utc(19, 14), recorded_at=utc(19, 14)),
            thread("after-midnight", occurred_at=utc(19, 16), recorded_at=utc(19, 16)),
            # Yesterday 00:00 Seoul is 11-18T15:00Z: the filter is inclusive there.
            thread("edge-in", occurred_at=utc(1, 0), recorded_at=utc(18, 15)),
            thread("edge-out", occurred_at=utc(1, 0), recorded_at=utc(18, 14, 59)),
        ]
    )
    view = ReadModel(h.config).now(utc(20, 10))
    assert view.today == "2026-11-20"
    assert [e.thread.thread_id for e in view.recent] == [
        "after-midnight",
        "before-midnight",
        "edge-in",
    ]
    seoul = [
        (
            e.thread.occurred_at.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
            e.thread.recorded_at.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
        )
        for e in view.recent[:2]
    ]
    assert seoul == [("2026-11-20", "2026-11-20"), ("2026-11-19", "2026-11-19")]


def test_now_endpoint_returns_the_same_shape(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("read")
    body = h.client.get(f"{READ}/now", headers=headers).json()
    assert set(body) == {
        "today",
        "overdue",
        "due_today",
        "recent",
        "head",
        "timezone",
        "fabric_epoch",
        "generated_at",
    }
    assert [e["thread"]["thread_id"] for e in body["overdue"]] == ["t1"]


def test_timeline_membership_counterpart_mention_and_area(tmp_path):
    h = Harness(tmp_path)
    seed(h)
    headers = h.pair("read")

    def rows(subject, **params):
        response = h.client.get(
            f"{READ}/timeline", params={"subject": subject, **params}, headers=headers
        )
        return [
            (r["thread"]["thread_id"], r["membership"])
            for r in response.json()["items"]
        ]

    # A person: threads where they are the counterpart (alias resolved, facts only).
    # (t5 was retracted but, as in the web, retracted facts stay visible.)
    assert rows("이서준") == [("t5", "counterpart"), ("t1", "counterpart")]
    # Mentions are opt-in and never counterpart rows: t3 mentions 이서준 via alias.
    assert rows("이서준", mentions="true") == [
        ("t5", "counterpart"),
        ("t3", "mention"),
        ("t1", "counterpart"),
    ]
    # The counterpart of t3 is 한지우, so for 한지우 it is a counterpart row, not a mention.
    assert ("t3", "counterpart") in rows("한지우")
    assert all(m == "counterpart" for _, m in rows("한지우", mentions="true"))
    # An area matches by tag; history is hidden unless requested, then follows its target.
    area = rows("네트워크")
    assert {k for k, _ in area} == {"t1", "t2", "t3", "t4", "t5", "d1"}
    with_history = dict(rows("네트워크", history="true"))
    assert {"c1", "j1", "r5", "wiki-field:네트워크:status"} <= set(with_history)
    # The correction row belongs to the person through the fact it changes.
    assert ("c1", "counterpart") in rows("이서준", history="true")
    retracted = h.client.get(f"{READ}/threads/t5", headers=headers).json()
    assert retracted["retracted"] is True and retracted["presentation"] == "fact"


def test_wiki_pages_list_detail_and_conditional_get(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    assert h.client.get(f"{READ}/wiki/pages", headers=headers).json()["items"] == []
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    store.write(
        "pages/방화벽 교체.md",
        {
            "kind": "project",
            "area": "네트워크",
            "status": "진행 중",
            "deadline": "2026-11-30",
            "assignees": ["나"],
        },
        "노후 장비를 교체한다.\n\n## 진행\n- 룰 3건",
        None,
    )
    store.write("pages/네트워크.md", {"kind": "area"}, "사내망 영역", None)
    store.write("Digest/2026-11.md", {}, "digest is not a page", None)
    pages = h.client.get(f"{READ}/wiki/pages", headers=headers).json()["items"]
    assert [p["title"] for p in pages] == ["네트워크", "방화벽 교체"]
    project = pages[1]
    assert (project["kind"], project["area"]) == ("project", "네트워크")
    assert pages[0]["area"] is None
    detail = h.client.get(f"{READ}/wiki/pages/{project['id']}", headers=headers)
    body = detail.json()
    assert body["title"] == "방화벽 교체" and "룰 3건" in body["body"]
    assert body["frontmatter"]["status"] == "진행 중"
    assert body["frontmatter"]["deadline"] == "2026-11-30"
    assert body["frontmatter"]["assignees"] == ["나"]
    assert "weaver" not in body["frontmatter"]
    assert detail.headers["etag"] == f'"{body["sha256"]}"'
    assert body["sha256"] == project["sha256"]
    same = h.client.get(
        f"{READ}/wiki/pages/{project['id']}",
        headers={**headers, "If-None-Match": detail.headers["etag"]},
    )
    assert same.status_code == 304
    assert (
        h.client.get(f"{READ}/wiki/pages/missing", headers=headers).json()["code"]
        == "page_not_found"
    )


def test_wiki_pending_human_edit_is_not_published_to_the_phone(tmp_path):
    h = Harness(tmp_path)
    headers = h.pair("read")
    store = WikiStore(
        h.config.vault_root, h.config.state_root, wiki_root=h.config.wiki.folder
    )
    store.write("pages/메모.md", {"kind": "topic"}, "원문", None)
    page = h.client.get(f"{READ}/wiki/pages", headers=headers).json()["items"][0]
    (store.root / "pages" / "메모.md").write_text(
        "---\nkind: topic\n---\n사람이 고친 중간 상태"
    )
    after = h.client.get(f"{READ}/wiki/pages/{page['id']}", headers=headers).json()
    assert "원문" in after["body"] and "중간" not in after["body"]
