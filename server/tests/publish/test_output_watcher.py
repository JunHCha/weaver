"""Output edits against synthetic temporary vaults, never user notes or models."""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from weaver.fabric import EntryQuery, FabricStore
from weaver.publish import AiFolder, OutputSync, PublishState, SqliteOutputWatcher
from weaver.publish.frontmatter import read_properties

ENTITY = "ent_" + "0" * 25 + "1"
PERSON = "ent_" + "0" * 25 + "2"
NOTE = "evt_" + "0" * 25 + "1"
SOURCE = "src_" + "0" * 25 + "1"


class Harness:
    def __init__(self, root: Path) -> None:
        self.vault = root / "vault"
        self.vault.mkdir()
        self.folder = AiFolder(self.vault, "Weaver")
        self.manifest = PublishState(root / "state" / "publish.sqlite3")
        self.path = root / "state" / "watcher.sqlite3"
        self.now = datetime(2026, 11, 7, 14, 59, tzinfo=UTC)
        self.watcher = self.restart()

    def restart(self) -> SqliteOutputWatcher:
        return SqliteOutputWatcher(
            self.folder,
            self.path,
            timezone=ZoneInfo("Asia/Seoul"),
            manifest=self.manifest,
            clock=lambda: self.now,
        )

    def tomorrow(self) -> None:
        self.now += timedelta(days=1)

    def write(self, name: str = "copy.md", **fields: object) -> Path:
        data = dict(
            weaver_schema_version=1,
            weaver_note_id=NOTE,
            weaver_source_ids=[SOURCE],
            weaver_entity_ids=[ENTITY],
            weaver_entry_ids=[NOTE],
            deadline="2026-12-01",
            assignees=[PERSON],
            status="open",
            decision="proceed",
        )
        data.update(fields)
        p = self.vault / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\n"
            + "\n".join(f"{k}: {json.dumps(v)}" for k, v in data.items())
            + "\n---\nBody ignored.\n"
        )
        return p

    def baseline(self) -> str:
        with sqlite3.connect(self.path) as db:
            return str(
                db.execute("SELECT baseline FROM registry LIMIT 1").fetchone()[0]
            )


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_first_discovery_and_all_fields(h: Harness) -> None:
    p = h.write()
    assert h.watcher.scan() == []
    before = h.baseline()
    h.write(deadline="2026-12-02", assignees=[], status="closed", decision="stop")
    assert h.watcher.scan() == []
    h.tomorrow()
    entries = h.watcher.scan()
    assert {e.field: e.value for e in entries} == {
        "deadline": "2026-12-02",
        "assignees": {"add": [], "remove": [PERSON]},
        "status": "closed",
        "decision": "stop",
    }
    assert all(
        e.actor == "human"
        and e.kind == "변경"
        and e.entities == [ENTITY]
        and e.source_id.startswith("output:")
        and e.resolves is None
        and e.supersedes is None
        and e.retracts is None
        for e in entries
    )
    assert h.baseline() == before
    h.watcher.acknowledge([entries[0].entry_id])
    assert h.baseline() == before
    assert len(h.watcher.scan()) == 3
    h.watcher.acknowledge([e.entry_id for e in entries])
    assert h.baseline() != before
    assert h.watcher.scan() == []
    assert p.exists()


def test_retries_restart_crash_after_append_and_checkpoint(
    h: Harness, tmp_path: Path
) -> None:
    h.write()
    h.watcher.scan()
    h.write(status="closed")
    h.tomorrow()
    first = h.watcher.scan()
    before = h.baseline()
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append(first)  # crash before acknowledgment
    restarted = h.restart()
    assert restarted.scan() == first
    assert h.baseline() == before
    OutputSync(restarted, fabric).run()
    assert fabric.entries(EntryQuery()) == first
    assert restarted.scan() == []
    assert h.baseline() != before


def test_append_failure_preserves_payload_and_later_edits(
    h: Harness, tmp_path: Path
) -> None:
    class FailingFabric(FabricStore):
        def append(self, entries: list[object]) -> None:
            raise OSError("synthetic failure")

    h.write()
    h.watcher.scan()
    before = h.baseline()
    h.write(status="closed")
    h.tomorrow()
    with pytest.raises(OSError):
        OutputSync(h.watcher, FailingFabric(tmp_path / "failed.sqlite3")).run()
    pending = h.watcher.scan()
    h.write(status="later")
    h.tomorrow()
    assert h.restart().scan() == pending
    assert h.baseline() == before
    h.watcher.acknowledge([e.entry_id for e in pending])
    h.tomorrow()
    assert [e.value for e in h.watcher.scan()] == ["later"]


def test_timezone_day_and_sleep_gap(h: Harness) -> None:
    h.write()
    h.watcher.scan()  # Seoul 23:59
    h.write(status="changed")
    h.now += timedelta(minutes=2)  # next local day, same UTC date
    entries = h.watcher.scan()
    assert len(entries) == 1
    h.watcher.acknowledge([e.entry_id for e in entries])
    h.write(status="after sleep")
    h.now += timedelta(days=8)
    assert len(h.restart().scan()) == 1


@pytest.mark.parametrize("field", ["deadline", "assignees", "status", "decision"])
@pytest.mark.parametrize("delete", [False, True])
def test_clear_fields(h: Harness, field: str, delete: bool) -> None:
    p = h.write()
    h.watcher.scan()
    if delete:
        p.write_text(
            "\n".join(
                line
                for line in p.read_text().splitlines()
                if not line.startswith(field + ":")
            )
            + "\n"
        )
    else:
        h.write(**{field: None})
    h.tomorrow()
    entries = h.watcher.scan()
    assert len(entries) == 1 and entries[0].field == field and entries[0].value is None


@pytest.mark.parametrize(
    "bad",
    [
        dict(deadline="tomorrow"),
        dict(status=5),
        dict(assignees=["unknown"]),
        dict(decision={}),
        dict(weaver_schema_version=99),
        dict(weaver_source_ids=[]),
        dict(weaver_entity_ids=[]),
        dict(weaver_entry_ids=["bad"]),
    ],
)
def test_invalid_does_not_advance(
    h: Harness, bad: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    h.write()
    h.watcher.scan()
    before = h.baseline()
    h.write(**bad)
    h.tomorrow()
    assert h.watcher.scan() == []
    assert h.baseline() == before
    assert "output skipped" in caplog.text
    h.write(status="valid again")
    h.tomorrow()
    assert [e.value for e in h.watcher.scan()] == ["valid again"]


def test_copy_move_delete_and_conflicting_values(h: Harness) -> None:
    first = h.write("one.md")
    second = h.write("two.md")
    h.watcher.scan()
    h.write("one.md", status="a")
    h.write("two.md", status="b")
    h.tomorrow()
    entries = h.watcher.scan()
    assert {e.value for e in entries} == {"a", "b"}
    assert len({e.source_id for e in entries}) == 2
    source = next(e.source_id for e in entries if e.value == "a")
    h.watcher.acknowledge([e.entry_id for e in entries])
    first.rename(h.vault / "moved.md")
    h.write("moved.md", status="c")
    h.tomorrow()
    changed = h.watcher.scan()
    assert len(changed) == 1 and changed[0].source_id == source
    h.watcher.acknowledge([e.entry_id for e in changed])
    second.unlink()
    h.tomorrow()
    assert h.watcher.scan() == []
    with sqlite3.connect(h.path) as db:
        assert (
            db.execute("SELECT count(*) FROM registry WHERE active=0").fetchone()[0]
            == 1
        )


def test_ambiguous_move_creates_new_baselines(h: Harness) -> None:
    p = h.write()
    h.watcher.scan()
    p.unlink()
    h.write("a.md", status="a")
    h.write("b.md", status="b")
    h.tomorrow()
    assert h.watcher.scan() == []
    with sqlite3.connect(h.path) as db:
        assert db.execute("SELECT count(*) FROM registry").fetchone()[0] == 3


def test_ignores_ai_temporary_symlinks_and_ordinary_body(
    h: Harness, tmp_path: Path
) -> None:
    for name in [
        "Weaver/ai.md",
        ".hidden.md",
        "~syncthing~copy.md",
        ".hidden/note.md",
        "a.md.tmp",
    ]:
        h.write(name)
    ordinary = h.vault / "ordinary.md"
    ordinary.write_bytes(b"ordinary note\n\xffnot valid UTF-8 or frontmatter\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("secret")
    (h.vault / "link").symlink_to(outside, target_is_directory=True)
    p = h.write()
    h.watcher.scan()
    p.write_text(p.read_text() + "different body\n")
    # Formatting/whitespace only is not a semantic edit. mtime is no authority.
    p.write_text(p.read_text().replace('"open"', "'  open  '"))
    os.utime(p, (1, 1))
    h.tomorrow()
    assert h.watcher.scan() == []
    with sqlite3.connect(h.path) as db:
        assert db.execute("SELECT count(*) FROM registry").fetchone()[0] == 1


def test_scan_and_append_never_write_vault_or_calendar(
    h: Harness, tmp_path: Path
) -> None:
    h.write()
    h.watcher.scan()
    h.write(status="closed")
    h.tomorrow()
    before = {p: p.read_bytes() for p in h.vault.rglob("*.md")}
    calendar = tmp_path / "calendar.json"
    calendar.write_text("[]")
    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    entries = h.watcher.scan()
    assert fabric.entries(EntryQuery()) == []
    assert entries
    OutputSync(h.watcher, fabric).run()
    assert {p: p.read_bytes() for p in before} == before
    assert calendar.read_text() == "[]"
    assert len(fabric.entries(EntryQuery())) == 1


def test_yaml_block_lists_and_null(h: Harness) -> None:
    p = h.write()
    p.write_text(
        p.read_text()
        .replace(json.dumps([SOURCE]), "\n  - " + SOURCE)
        .replace('status: "open"', "status:")
    )
    assert read_properties(p)["status"] is None
    assert h.watcher.scan() == []


def test_repeated_transition_is_new_fact(h: Harness) -> None:
    h.write()
    h.watcher.scan()
    ids = []
    for status in ["closed", "open", "closed"]:
        h.tomorrow()
        h.write(status=status)
        entries = h.watcher.scan()
        ids.append(entries[0].entry_id)
        h.watcher.acknowledge([e.entry_id for e in entries])
    assert len(set(ids)) == 3


def test_reject_state_in_vault_and_unknown_ack(h: Harness) -> None:
    with pytest.raises(ValueError, match="outside vault"):
        SqliteOutputWatcher(
            h.folder,
            h.vault / "state.db",
            timezone=ZoneInfo("UTC"),
            manifest=h.manifest,
        )
    with pytest.raises(ValueError, match="unknown"):
        h.watcher.acknowledge(["evt_unknown"])


def test_manifest_excludes_publisher_paths_after_folder_change(h: Harness) -> None:
    from publish.test_publisher import entry
    from weaver.publish.notes import render_entry

    note = render_entry(
        entry(1, field="status", value="open"), "유효", {}, ZoneInfo("UTC")
    )
    path = h.vault / "OldAI" / (note.note_id + ".md")
    path.parent.mkdir()
    path.write_bytes(note.data)
    h.manifest.apply(
        [(note, path.relative_to(h.vault).as_posix())], [(note.note_id, "human_owned")]
    )
    assert h.watcher.scan() == []
    path.write_bytes(note.data.replace(b"open", b"changed"))
    h.tomorrow()
    assert h.watcher.scan() == []
    with sqlite3.connect(h.path) as db:
        assert db.execute("SELECT count(*) FROM registry").fetchone()[0] == 0


def test_publisher_copy_shared_fields_round_trip(h: Harness, tmp_path: Path) -> None:
    from publish.test_publisher import entry
    from weaver.publish import Publisher
    from weaver.schema.internal import EntityIndex, IndexedEntity

    class Names:
        def index(self) -> EntityIndex:
            return EntityIndex(
                entities=[
                    IndexedEntity(
                        entity_id=ENTITY, type="project", name="Synthetic", aliases=[]
                    )
                ]
            )

    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append(
        [
            entry(1, field="deadline", value="2026-12-01"),
            entry(2, field="assignees", value={"add": [PERSON], "remove": []}),
            entry(3, field="status", value="open"),
            entry(4, field="decision", value="proceed"),
        ]
    )
    publisher = Publisher(
        h.folder, fabric, Names(), state=h.manifest, timezone=ZoneInfo("UTC")
    )
    publisher.publish([ENTITY])
    original = h.folder.root / "Projects" / (ENTITY + ".md")
    copy = h.vault / "copy.md"
    copy.write_bytes(original.read_bytes())
    properties = read_properties(copy)
    assert properties["deadline"] == "2026-12-01"
    assert properties["assignees"] == [PERSON]
    assert properties["decision"] == "proceed"
    assert h.watcher.scan() == []
    copy.write_text(copy.read_text().replace('status: "open"', 'status: "closed"'))
    h.tomorrow()
    OutputSync(h.watcher, fabric).run()
    assert fabric.state(ENTITY).fields["status"].value == "closed"
    assert original.read_bytes() != copy.read_bytes()
    assert read_properties(original)["status"] == "open"


def test_concurrent_scans_have_identical_durable_delivery(h: Harness) -> None:
    from concurrent.futures import ThreadPoolExecutor

    h.write()
    h.watcher.scan()
    h.write(status="closed")
    h.tomorrow()
    watchers = [h.restart() for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda w: w.scan(), watchers))
    assert results[0] == results[1]
    assert len(results[0]) == 1


def test_pending_deleted_output_still_delivers(h: Harness) -> None:
    p = h.write()
    h.watcher.scan()
    h.write(status="closed")
    h.tomorrow()
    entries = h.watcher.scan()
    p.unlink()
    h.tomorrow()
    assert h.restart().scan() == entries
    h.watcher.acknowledge([e.entry_id for e in entries])
    assert h.watcher.scan() == []


@pytest.mark.parametrize(
    "formatted",
    ["'open' # unchanged", '"open" # unchanged', "open # unchanged"],
)
def test_inline_comments_do_not_create_facts(h: Harness, formatted: str) -> None:
    p = h.write()
    h.watcher.scan()
    before = h.baseline()
    p.write_text(
        p.read_text()
        .replace('status: "open"', f"status: {formatted}")
        .replace('deadline: "2026-12-01"', "deadline: 2026-12-01 # same date")
    )
    h.tomorrow()
    assert h.watcher.scan() == []
    assert h.baseline() == before


def test_commented_null_clears_field(h: Harness) -> None:
    p = h.write()
    h.watcher.scan()
    p.write_text(p.read_text().replace('status: "open"', "status: null # clear"))
    h.tomorrow()
    entries = h.watcher.scan()
    assert len(entries) == 1
    assert entries[0].field == "status" and entries[0].value is None


@pytest.mark.parametrize(
    ("formatted", "value"),
    [
        ("'open # still open' # comment", "open # still open"),
        ('"open # still open" # comment', "open # still open"),
        ("'it''s # open' # comment", "it's # open"),
        ('"say \\"open\\" # now" # comment', 'say "open" # now'),
        ("open#now # comment", "open#now"),
    ],
)
def test_hash_inside_scalar_is_preserved(
    h: Harness, formatted: str, value: str
) -> None:
    p = h.write()
    h.watcher.scan()
    p.write_text(p.read_text().replace('status: "open"', f"status: {formatted}"))
    assert read_properties(p)["status"] == value
    h.tomorrow()
    entries = h.watcher.scan()
    assert len(entries) == 1 and entries[0].value == value


@pytest.mark.parametrize("indent", ["", "  "])
def test_block_assignees_preserve_fabric(
    h: Harness, tmp_path: Path, indent: str
) -> None:
    from publish.test_publisher import entry

    fabric = FabricStore(tmp_path / "fabric.sqlite3")
    fabric.append([entry(1, field="assignees", value={"add": [PERSON], "remove": []})])
    before_fabric = fabric.state(ENTITY)
    p = h.write()
    h.watcher.scan()
    before_baseline = h.baseline()
    p.write_text(
        p.read_text().replace(
            f"assignees: {json.dumps([PERSON])}",
            f"assignees: # same people\n{indent}- {PERSON} # unchanged",
        )
    )
    assert read_properties(p)["assignees"] == [PERSON]
    h.tomorrow()
    assert h.watcher.scan() == []
    OutputSync(h.watcher, fabric).run()
    assert h.baseline() == before_baseline
    assert fabric.state(ENTITY) == before_fabric


@pytest.mark.parametrize(
    "unsupported",
    [
        "status: parent\n  child: nested",
        "status:\n  child: nested",
        'status: ["open", ["nested"]]',
        "status: {child: nested}",
        "status:\n- child: nested",
        "status:\n- open\n  - nested",
        'status:\n- ["nested"]',
        "status: |\n  multiline",
        "status: >-\n  multiline",
        "status: 'unclosed # quote",
        "status: 'open' trailing",
        "status: &anchor open",
        "status: *anchor",
        "status: !tag open",
        "status: yes # implicit boolean",
        "status: 1.5 # numeric scalar",
        "status: 0x10",
        "status: -",
        "status: open\nstatus: closed",
        "status: open\ncustom: one\ncustom: two",
        "status: closed\ncustom:\n  child: nested",
        'status: closed\ncustom: ["one", {"child": "nested"}]',
        "status: closed\ncustom: plain\n  continuation",
    ],
)
def test_unsupported_frontmatter_rejects_whole_snapshot(
    h: Harness, unsupported: str, caplog: pytest.LogCaptureFixture
) -> None:
    p = h.write()
    h.watcher.scan()
    before = h.baseline()
    p.write_text(p.read_text().replace('status: "open"', unsupported))
    with pytest.raises(ValueError):
        read_properties(p)
    h.tomorrow()
    assert h.watcher.scan() == []
    assert h.baseline() == before
    assert "output skipped: ValueError" in caplog.text
    h.write(status="valid again")
    h.tomorrow()
    assert [e.value for e in h.watcher.scan()] == ["valid again"]
