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
    assert store.resolve("덮어쓰기") is None
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
