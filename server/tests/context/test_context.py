from pathlib import Path

import pytest

from weaver.context import ContextDictionary, Page, SchemaVersionError


def test_jamo_candidates_and_limit(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages(
        [
            Page("홍길동", "person"),
            Page("김철수", "person"),
            Page("푸른나무", "project"),
        ]
    )
    assert [x.name for x in store.candidates("홍길돈에게 연락")] == ["홍길동"]
    assert [x.name for x in store.candidates("홍길둥과 회의")] == ["홍길동"]
    assert [x.name for x in store.candidates("홍갈돈에게 연락")] == ["홍길동"]
    assert [x.name for x in store.candidates("홍길동 김철수", limit=1)] == ["김철수"]
    assert store.candidates("무관한 문장") == []
    assert store.candidates("홍길동", limit=0) == []


def test_contact_is_first_even_when_absent(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("홍길동", "person")])
    result = store.candidates("홍길동", contact_name="김철수", limit=1)
    assert [(x.name, x.source) for x in result] == [("김철수", "contact")]


def test_learn_idempotent_and_latest_collision_wins(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.learn("홍길돈", "홍길동", "person")
    store.learn("홍길돈", "홍길동", "person")
    assert store.candidates("홍길돈")[0].misrecognitions == ("홍길돈",)
    store.learn("홍길돈", "김철수", "person")
    result = store.candidates("홍길돈")
    assert result[0].name == "김철수"
    assert all("홍길돈" not in x.misrecognitions for x in result[1:])
    store.learn("홍길돈", "김철수", "person")
    assert ContextDictionary(tmp_path).candidates("홍길돈")[0].name == "김철수"


def test_sync_updates_and_deletes_only_wiki_contributions(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.learn("홍길돈", "홍길동", "person")
    store.sync_from_pages(
        [Page("홍길동", "person", ("길동",), "첫 설명"), Page("김철수", "person")]
    )
    assert store.candidates("길동")[0].aliases == ("길동",)
    store.sync_from_pages([Page("홍길동", "project", ("새별명",), "새 설명")])
    entry = store.candidates("홍길돈")[0]
    assert (entry.kind, entry.summary, entry.aliases) == (
        "project",
        "새 설명",
        ("새별명",),
    )
    assert store.candidates("김철수") == []
    store.sync_from_pages([])
    entry = store.candidates("홍길돈")[0]
    assert (entry.kind, entry.summary, entry.aliases, entry.source) == (
        "person",
        "",
        (),
        "learn",
    )


def test_invalid_input_and_version(tmp_path: Path) -> None:
    import sqlite3

    store = ContextDictionary(tmp_path)
    with pytest.raises(ValueError):
        store.learn("", "홍길동", "person")
    with pytest.raises(ValueError):
        store.candidates("text", limit=-1)
    with pytest.raises(ValueError):
        store.sync_from_pages([Page("홍길동", "person"), Page("홍길동", "person")])
    with sqlite3.connect(tmp_path / "context.sqlite3") as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaVersionError):
        ContextDictionary(tmp_path)


def test_concurrent_learning_and_atomic_invalid_sync(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("홍길동", "person")])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: store.learn("홍길돈", "홍길동", "person"), range(20)))
    with pytest.raises(ValueError):
        store.sync_from_pages([Page("김철수", "person"), Page("", "person")])
    assert store.candidates("홍길돈")[0].misrecognitions == ("홍길돈",)
    assert store.candidates("홍길동")[0].source == "wiki"


def test_learned_correction_precedes_colliding_wiki_alias(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("김철수", "person", ("홍길돈",))])
    store.learn("홍길돈", "홍길동", "person")
    assert store.candidates("홍길돈")[0].name == "홍길동"
