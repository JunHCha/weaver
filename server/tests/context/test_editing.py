"""Synthetic v1 migration and durable public dictionary editing contract."""

import sqlite3
import unicodedata
from pathlib import Path

import pytest

from weaver.context import ContextDictionary, Page


def test_v1_upgrade_preserves_rows_and_readonly(tmp_path: Path) -> None:
    path = tmp_path / "context.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE wiki(name TEXT PRIMARY KEY, kind TEXT, summary TEXT);
            CREATE TABLE wiki_alias(name TEXT, alias TEXT, PRIMARY KEY(name,alias));
            CREATE TABLE learned(name TEXT PRIMARY KEY, kind TEXT);
            CREATE TABLE corrections(observed TEXT PRIMARY KEY, name TEXT);
            INSERT INTO wiki VALUES ('한글','project','설명');
            INSERT INTO wiki_alias VALUES ('한글','Alias');
            INSERT INTO learned VALUES ('사람','person');
            INSERT INTO corrections VALUES ('사람오타','사람');
            PRAGMA user_version=1;
        """)
        before = {
            t: db.execute(f"SELECT * FROM {t}").fetchall()
            for t in ("wiki", "wiki_alias", "learned", "corrections")
        }
    assert ContextDictionary.open_readonly(path).kind_of("ALIAS") == "project"
    for _ in range(2):
        store = ContextDictionary(tmp_path)
        assert store.resolve(unicodedata.normalize("NFD", "한글")).summary == "설명"
        assert store.candidates("사람오타")[0].name == "사람"
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (2,)
            for table, rows in before.items():
                assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    assert ContextDictionary.open_readonly(path).kind_of("alias") == "project"


def test_editing_survives_wiki_sync_and_restart(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("사건 제목", "topic", ("별명",), "위키 요약")])
    store.learn("사건오타", "사건 제목", "topic")
    entry = store.rename("사건 제목", "한글")
    assert entry.human_edited
    store.set_kind("한글", "project")
    store.add_alias("한글", "Alias")
    store.remove_alias("한글", "별명")
    store.sync_from_pages([Page("사건 제목", "person", ("덮어쓰기",))])
    store = ContextDictionary(tmp_path)
    assert store.resolve("사건 제목").name == "한글"
    assert store.kind_of("ALIAS") == "project"
    assert store.resolve("별명") is None
    assert store.resolve("덮어쓰기").name == "한글"
    assert store.candidates("사건오타")[0].name == "한글"
    assert len(store.list_entries()) == 1
    assert store.add_entry("alias", "person").name == "한글"
    store.add_entry("대상", "organization", ("Target",), human_edited=False)
    merged = store.merge("한글", "대상")
    assert merged.human_edited
    assert store.resolve("사건 제목").name == "대상"
    assert store.kind_of("ALIAS") == "organization"
    assert store.candidates("사건오타")[0].name == "대상"
    store.sync_from_pages([])
    assert ContextDictionary(tmp_path).resolve("한글").name == "대상"


def test_collision_and_normalization(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    nfd = unicodedata.normalize("NFD", "한글")
    assert store.add_entry(nfd, "topic", ("MixedCase",)).name == "한글"
    assert store.resolve("mixedcase").name == "한글"
    assert store.add_entry("한글", "person").kind == "topic"
    store.add_entry("다른", "person")
    for operation in (
        lambda: store.add_alias("다른", "MIXEDCASE"),
        lambda: store.rename("다른", "mixedcase"),
        lambda: store.set_kind("다른", "invalid"),
        lambda: store.add_alias("다른", " "),
    ):
        with pytest.raises(ValueError):
            operation()
    assert store.kind_of("다른") == "person"


def test_reassigned_correction_after_rename_and_merge(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.learn("오타", "이전", "person")
    store.rename("이전", "이름")
    store.add_entry("대상", "project")
    store.merge("이름", "대상")
    store.learn("오타", "다른", "person")
    assert store.resolve("대상").misrecognitions == ()
    assert store.resolve("다른").misrecognitions == ("오타",)


def test_alias_contact_is_not_duplicated(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.add_entry("표준이름", "person", ("Alias",))
    entries = store.candidates("표준이름", contact_name="alias")
    assert [e.name for e in entries] == ["표준이름"]


def test_auto_entry_yields_all_wiki_fields(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.add_entry("자동", "topic", ("자동별칭",), human_edited=False)
    store.sync_from_pages([Page("자동", "person", ("위키별칭",), "현재 요약")])
    entry = store.resolve("자동")
    assert (entry.kind, entry.summary, entry.aliases) == (
        "person",
        "현재 요약",
        ("위키별칭",),
    )
    assert not entry.human_edited
    store.sync_from_pages([])
    assert store.resolve("자동").kind == "topic"


def test_human_edits_only_override_selected_fields(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("원본", "topic", ("삭제", "유지"), "옛 요약")])
    store.rename("원본", "표준")
    store.add_alias("표준", "직접추가")
    store.remove_alias("표준", "삭제")
    store.sync_from_pages(
        [Page("원본", "person", ("삭제", "유지", "위키추가"), "새 요약")]
    )
    entry = store.resolve("표준")
    assert entry.kind == "person"
    assert entry.summary == "새 요약"
    assert set(entry.aliases) == {"원본", "유지", "위키추가", "직접추가"}
    store.set_kind("표준", "project")
    store.sync_from_pages([Page("원본", "organization", ("또추가",), "최신")])
    entry = store.resolve("표준")
    assert entry.kind == "project"
    assert entry.summary == "최신"
    assert set(entry.aliases) == {"원본", "또추가", "직접추가"}
    with sqlite3.connect(tmp_path / "context.sqlite3") as db:
        payload = db.execute("SELECT payload FROM edited").fetchone()[0]
    assert "최신" not in payload and "또추가" not in payload


def test_new_canonical_wiki_page_beats_existing_edited_alias(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("첫페이지", "person")])
    store.add_alias("첫페이지", "다른페이지")
    store.sync_from_pages(
        [Page("첫페이지", "person"), Page("다른페이지", "project", summary="독립 요약")]
    )
    assert [e.name for e in store.list_entries()] == ["다른페이지", "첫페이지"]
    assert store.resolve("다른페이지").kind == "project"
    assert store.resolve("다른페이지").summary == "독립 요약"
    store.set_kind("첫페이지", "topic")
    assert store.resolve("다른페이지").kind == "project"


def test_removed_old_name_resurrects_wiki_and_deleted_wiki_cleans_edit(
    tmp_path: Path,
) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("원본", "person")])
    store.rename("원본", "새이름")
    assert [e.name for e in store.list_entries()] == ["새이름"]
    store.remove_alias("새이름", "원본")
    assert {e.name for e in store.list_entries()} == {"새이름", "원본"}
    store.sync_from_pages([])
    assert store.list_entries() == []
    store.sync_from_pages([Page("원본", "project")])
    assert store.resolve("원본").kind == "project"
    assert store.resolve("새이름") is None


def test_concurrent_edits_do_not_lose_updates(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = ContextDictionary(tmp_path)
    store.add_entry("원본", "topic", tuple(f"삭제{i}" for i in range(5)))
    for i in range(5):
        store.add_entry(f"합침{i}", "person")
    operations = [lambda i=i: store.add_alias("원본", f"추가{i}") for i in range(10)]
    operations += [lambda i=i: store.remove_alias("원본", f"삭제{i}") for i in range(5)]
    operations += [lambda i=i: store.merge(f"합침{i}", "원본") for i in range(5)]
    operations += [
        lambda: store.rename("원본", "표준"),
        lambda: store.set_kind("원본", "project"),
    ]
    # Merge retains the target kind; the sole set_kind is never reverted by a stale read.
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda op: op(), operations))
    entry = store.resolve("원본")
    assert entry.name == "표준"
    assert entry.kind == "project"
    assert set(entry.aliases) == {
        "원본",
        *(f"추가{i}" for i in range(10)),
        *(f"합침{i}" for i in range(5)),
    }


def _v1_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE wiki(name TEXT PRIMARY KEY, kind TEXT, summary TEXT);
            CREATE TABLE wiki_alias(name TEXT, alias TEXT, PRIMARY KEY(name,alias));
            CREATE TABLE learned(name TEXT PRIMARY KEY, kind TEXT);
            CREATE TABLE corrections(observed TEXT PRIMARY KEY, name TEXT);
            INSERT INTO wiki VALUES ('보존','person','설명');
            INSERT INTO learned VALUES ('학습','topic');
            INSERT INTO corrections VALUES ('오타','학습');
            PRAGMA user_version=1;
        """)


def test_interrupted_upgrade_rolls_back_everything(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "context.sqlite3"
    _v1_database(path)

    def interrupted(db):
        db.execute("CREATE TABLE edited(name TEXT PRIMARY KEY, payload TEXT)")
        raise RuntimeError("synthetic migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(ContextDictionary, "_upgrade", staticmethod(interrupted))
        with pytest.raises(RuntimeError, match="synthetic"):
            ContextDictionary(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='edited'").fetchone()
            is None
        )
        assert db.execute("SELECT * FROM wiki").fetchall() == [
            ("보존", "person", "설명")
        ]
        assert db.execute("SELECT * FROM corrections").fetchall() == [("오타", "학습")]
    path.chmod(0o664)
    assert ContextDictionary(tmp_path).kind_of("보존") == "person"
    assert path.stat().st_mode & 0o777 == 0o600


def test_concurrent_open_during_upgrade(tmp_path: Path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    path = tmp_path / "context.sqlite3"
    _v1_database(path)
    entered, release = Event(), Event()
    upgrade = ContextDictionary._upgrade

    def paused(db):
        upgrade(db)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(ContextDictionary, "_upgrade", staticmethod(paused))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ContextDictionary, tmp_path)
        try:
            assert entered.wait(5)
            second = pool.submit(ContextDictionary, tmp_path)
            # Reader sees the intact v1 snapshot until the upgrade commits.
            assert ContextDictionary.open_readonly(path).kind_of("保留") is None
            assert ContextDictionary.open_readonly(path).kind_of("보존") == "person"
            assert not second.done()
        finally:
            release.set()
        assert first.result().kind_of("보존") == "person"
        assert second.result().kind_of("학습") == "topic"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)


def test_merged_wiki_aliases_stay_live(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.sync_from_pages(
        [
            Page("출처", "person", ("변경전", "삭제"), "출처 요약"),
            Page("대상", "project", summary="대상 요약"),
        ]
    )
    store.remove_alias("출처", "삭제")
    store.add_alias("출처", "직접")
    store.merge("출처", "대상")
    store.sync_from_pages(
        [
            Page("출처", "person", ("변경후", "삭제"), "새 출처 요약"),
            Page("대상", "topic", summary="새 대상 요약"),
        ]
    )
    entry = store.resolve("대상")
    assert entry.kind == "project"
    assert entry.summary == "새 대상 요약"
    assert set(entry.aliases) == {"출처", "변경후", "직접"}


def test_wiki_deletion_preserves_renamed_learned_corrections(tmp_path: Path) -> None:
    store = ContextDictionary(tmp_path)
    store.learn("오타", "원본", "person")
    store.sync_from_pages([Page("원본", "project", ("위키별칭",))])
    store.rename("원본", "표준")
    store.sync_from_pages([])
    entry = store.resolve("표준")
    assert (entry.kind, entry.misrecognitions, entry.source) == (
        "person",
        ("오타",),
        "learn",
    )
    assert store.candidates("오타")[0].name == "표준"
    assert store.resolve("원본") is None
    with sqlite3.connect(tmp_path / "context.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM edited").fetchone() == (0,)


def test_automatic_alias_respects_human_removal_and_does_not_freeze_wiki(tmp_path):
    store = ContextDictionary(tmp_path)
    store.sync_from_pages([Page("자동별칭", "topic")])
    assert not store.add_alias("자동별칭", "짧은이름", human_edited=False).human_edited
    store.remove_alias("자동별칭", "짧은이름")
    assert store.add_alias("자동별칭", "짧은이름", human_edited=False).human_edited
    assert store.resolve("짧은이름") is None
    store.sync_from_pages([Page("자동별칭", "project", summary="변경")])
    store = ContextDictionary(tmp_path)
    assert store.resolve("자동별칭").kind == "project"
    assert store.resolve("짧은이름") is None
    store.add_entry("별도", "person", ("이미있음",))
    with pytest.raises(ValueError, match="collision"):
        store.add_alias("자동별칭", "이미있음", human_edited=False)


def test_whitespace_resolution_collisions_and_automatic_rename(tmp_path):
    import unicodedata

    import pytest

    from weaver.context import ContextEntry, DictionarySnapshot

    context = ContextDictionary(tmp_path)
    context.add_entry("홍길동", "person", human_edited=False)
    assert context.resolve(" 홍\t길 동\n").name == "홍길동"
    context.add_entry("한빛", "organization", ("Bright Lab",))
    assert context.resolve("BRIGHT\tLAB").name == "한빛"
    with pytest.raises(ValueError):
        context.add_alias("홍길동", "brightlab")
    with pytest.raises(ValueError):
        context.sync_from_pages([Page("한 빛", "topic"), Page("한빛", "topic")])
    snapshot = DictionarySnapshot(
        [
            ContextEntry("Z", "topic", ("A B",)),
            ContextEntry("AB", "person"),
            ContextEntry("A", "topic", ("a b",)),
        ]
    )
    assert snapshot.resolve("a\tb").name == "AB"
    collision = DictionarySnapshot(
        [ContextEntry("Z", "topic", ("A B",)), ContextEntry("A", "topic", ("ab",))]
    )
    assert collision.resolve("a b").name == "A"
    context.add_entry(
        "지원팀 김철수 팀장님", "person", ("지원팀 김철수 팀장님",), human_edited=False
    )
    renamed = context.rename("지원팀 김철수 팀장님", "김철수", human_edited=False)
    assert not renamed.human_edited and not renamed.aliases
    assert renamed.misrecognitions == ("지원팀 김철수 팀장님",)
    assert context.resolve("지원팀 김철수 팀장님") is None
    assert context.resolve(unicodedata.normalize("NFD", "김 철 수")).name == "김철수"
    assert context.resolve("김철수").misrecognitions == ("지원팀 김철수 팀장님",)
    context.set_kind("김철수", "person")
    with pytest.raises(ValueError):
        context.rename("김철수", "김영희", human_edited=False)
