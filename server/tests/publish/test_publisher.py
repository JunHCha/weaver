"""Publisher (#7) against temporary vaults and Fabric DBs only; never a real vault."""

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from weaver.config import ConfigError, load_config
from weaver.fabric import FabricStore
from weaver.loom.review_store import ReviewStore
from weaver.publish import (
    AiFolder,
    Publisher,
    PublishPathError,
    PublishState,
    RelationLink,
    ReviewRelations,
    VaultSync,
    embedded_hash,
)
from weaver.runtime.assemble import vault_publisher
from weaver.schema import Entry
from weaver.schema.internal import EntityIndex, IndexedEntity

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOTS = Path(__file__).parent / "snapshots"
ZONE = ZoneInfo("Asia/Seoul")
P, K, O, X = (f"ent_{n:026d}" for n in (1, 2, 3, 9))  # noqa: E741
SRC = f"src_{1:026d}"


def eid(n: int) -> str:
    return f"evt_{n:026d}"


def entry(n: int, **changes: object) -> Entry:
    base: dict[str, object] = dict(
        schema_version=1,
        entry_id=eid(n),
        entities=[P],
        kind="변경",
        field=None,
        value=None,
        occurred_at=datetime(2026, 11, 7, 1, n, tzinfo=UTC),
        recorded_at=datetime(2026, 11, 7, 2, n, tzinfo=UTC),
        source_id=SRC,
        actor="ai_approved",
        supersedes=None,
        retracts=None,
        request=None,
        resolves=None,
        blocked_by_request=None,
    )
    return Entry.model_validate(base | changes)


def project_a() -> list[Entry]:
    """Fake project A: a request, its human decision, a replaced deadline."""
    return [
        entry(
            1,
            kind="요청",
            entities=[P, K],
            value="견적서",
            request={
                "requester": K,
                "targets": [P],
                "due_at": "2026-11-20T18:00:00+09:00",
                "content": "견적서를 금요일까지 보내 주세요",
            },
        ),
        entry(2, field="deadline", value="2026-11-30"),
        entry(
            3,
            kind="결정",
            entities=[P, K],
            value="견적서는 금요일에 발송",
            actor="human",
            resolves=eid(1),
        ),
        entry(4, field="deadline", value="2026-12-05", supersedes=eid(2)),
        entry(5, kind="약속", field="assignees", value={"add": [K], "remove": []}),
        entry(6, entities=[O], kind="논의", value="가나다 상사 내부 검토"),
    ]


class Names:
    def __init__(self) -> None:
        self.entities = [
            IndexedEntity(entity_id=P, type="project", name="프로젝트 A", aliases=[]),
            IndexedEntity(
                entity_id=K, type="person", name="김 과장", aliases=["김과장"]
            ),
            IndexedEntity(entity_id=O, type="org", name="가나다 상사", aliases=[]),
        ]

    def index(self) -> EntityIndex:
        return EntityIndex(entities=self.entities)


class Relations:
    def __init__(self) -> None:
        self.items = [
            RelationLink("소속", (K,), (O,), "가나다 상사 영업 담당", SRC),
            RelationLink(
                "후속조치", (P,), (eid(1), eid(3)), "프로젝트 A의 다음 작업", SRC
            ),
        ]

    def relations(self) -> list[RelationLink]:
        return self.items


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.vault = tmp_path / "vault"
        (self.vault / "Daily").mkdir(parents=True)
        (self.vault / "Projects" / "프로젝트 A").mkdir(parents=True)
        self.daily = self.vault / "Daily" / "2026-11-07.md"
        self.daily.write_text("# 오늘\n사람이 쓴 데일리\n")
        self.para = self.vault / "Projects" / "프로젝트 A" / "index.md"
        self.para.write_text("사람이 쓴 PARA 노트\n")
        self.fabric = FabricStore(tmp_path / "state" / "fabric.sqlite3")
        self.fabric.append(project_a())
        self.names = Names()
        self.relations = Relations()
        self.state = PublishState(tmp_path / "state" / "publish.sqlite3")
        self.publisher = Publisher(
            AiFolder(self.vault, "Weaver"),
            self.fabric,
            self.names,
            relations=self.relations,
            state=self.state,
            timezone=ZONE,
        )
        self.ai = self.vault / "Weaver"

    def note(self, entity_or_entry: str) -> Path:
        [path] = self.ai.rglob(f"{entity_or_entry}.md")
        return path

    def files(self) -> dict[str, bytes]:
        return {
            p.relative_to(self.vault).as_posix(): p.read_bytes()
            for p in sorted(self.vault.rglob("*"))
            if p.is_file()
        }


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def rel(world: World, paths: list[Path]) -> list[str]:
    return [p.relative_to(world.vault).as_posix() for p in paths]


def test_project_a_snapshot(world: World) -> None:
    written = world.publisher.publish([P, K, O, P])
    assert rel(world, written) == [
        "Weaver/Projects/" + P + ".md",
        *(f"Weaver/Records/2026-11/{eid(n)}.md" for n in range(1, 6)),
        "Weaver/Entities/People/" + K + ".md",
        "Weaver/Entities/Orgs/" + O + ".md",
        f"Weaver/Records/2026-11/{eid(6)}.md",
    ]
    for name, path in [
        ("project_moc.md", world.note(P)),
        ("person.md", world.note(K)),
        ("record_decision.md", world.note(eid(3))),
        ("record_superseded.md", world.note(eid(2))),
    ]:
        expected = SNAPSHOTS / name
        if os.environ.get("WEAVER_UPDATE_SNAPSHOTS"):
            expected.write_bytes(path.read_bytes())
        assert path.read_bytes() == expected.read_bytes(), name
    # Daily link is from AI notes to the human daily note, never the reverse.
    assert "- 데일리:: [[2026-11-07]]" in world.note(eid(3)).read_text()


def dataview_fields(text: str) -> dict[str, list[str]]:
    """Dataview's unbracketed rule: ``Key:: value`` up to the end of the line,
    at line start or after a list marker; code fences are not metadata.
    """
    fields: dict[str, list[str]] = {}
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        match = re.match(r"^(?:- )?([^\s:*`\[\]][^:]*?)::\s*(.*)$", line)
        if not fenced and match:
            fields.setdefault(match.group(1), []).append(match.group(2))
    return fields


def frontmatter(text: str) -> str:
    return text.split("\n---\n", 1)[0]


def test_relation_kind_reason_and_source_ids_are_queryable(world: World) -> None:
    world.publisher.publish([P, K])
    project = dataview_fields(world.note(P).read_text())
    assert project["후속조치"] == [
        f"[[{eid(1)}]], [[{eid(3)}]] — 프로젝트 A의 다음 작업"
    ]
    person = dataview_fields(world.note(K).read_text())
    assert person["소속"] == [f"[[{O}|가나다 상사]] — 가나다 상사 영업 담당"]
    record = world.note(eid(3)).read_text()
    fields = dataview_fields(record)
    assert fields["원본"] == [SRC]
    assert fields["해결"] == [f"[[{eid(1)}]]"]
    head = frontmatter(record)
    assert f'weaver_note_id: "{eid(3)}"' in head
    assert f'weaver_source_ids:\n  - "{SRC}"' in head
    assert f'weaver_entity_ids:\n  - "{P}"\n  - "{K}"' in head
    # Entity note frontmatter carries every contributing entry and source ID.
    moc = frontmatter(world.note(P).read_text())
    for n in range(1, 6):
        assert f'  - "{eid(n)}"' in moc
    assert embedded_hash(world.note(P).read_bytes()) is not None


def test_rerun_writes_nothing_and_creates_no_duplicates(world: World) -> None:
    world.publisher.publish([P, K, O])
    before = world.files()
    mtimes = {p: p.stat().st_mtime_ns for p in world.ai.rglob("*.md")}
    assert world.publisher.publish([P, K, O]) == []
    assert world.files() == before
    assert {p: p.stat().st_mtime_ns for p in world.ai.rglob("*.md")} == mtimes
    assert world.note(P).read_text().count(f"[[{eid(3)}]]") == 3


def test_new_fact_rewrites_only_needed_notes(world: World) -> None:
    world.publisher.publish([P, K, O])
    before = world.files()
    world.fabric.append([entry(7, field="status", value="진행")])
    written = rel(world, world.publisher.publish([P]))
    assert written == [f"Weaver/Projects/{P}.md", f"Weaver/Records/2026-11/{eid(7)}.md"]
    after = world.files()
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed == set(written)


def test_unrelated_daily_para_and_human_copies_untouched(world: World) -> None:
    world.publisher.publish([K])
    assert not list(world.ai.rglob(f"{O}.md"))  # O was not in changed
    copy = world.vault / "Projects" / "프로젝트 A" / "김 과장 메모.md"
    shutil.copy(world.note(K), copy)
    copy.write_text(copy.read_text().replace("# 김 과장", "# 김 과장 (사람 수정)"))
    daily, para = world.daily.read_bytes(), world.para.read_bytes()
    copied = copy.read_bytes()
    world.fabric.append([entry(7, entities=[K], field="phone", value="010")])
    world.publisher.publish([K, P])
    assert world.daily.read_bytes() == daily
    assert world.para.read_bytes() == para
    assert copy.read_bytes() == copied
    assert embedded_hash(copied) is None  # OutputWatcher sees the edit


def test_human_edited_or_foreign_ai_note_is_never_overwritten(world: World) -> None:
    world.publisher.publish([P, K])
    edited = world.note(K)
    edited.write_text(edited.read_text() + "\n사람이 덧붙인 줄\n")
    foreign = world.ai / "Entities" / "Orgs" / f"{O}.md"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("사람이 먼저 만든 같은 이름의 파일\n")
    kept = edited.read_bytes(), foreign.read_bytes()
    world.fabric.append([entry(7, entities=[K, O], field="phone", value="010")])
    written = world.publisher.publish([K, O])
    assert (edited.read_bytes(), foreign.read_bytes()) == kept
    assert edited not in written and foreign not in written
    assert {(c.path, c.reason) for c in world.publisher.conflicts} == {
        (f"Weaver/Entities/People/{K}.md", "human_owned"),
        (f"Weaver/Entities/Orgs/{O}.md", "human_owned"),
    }
    assert world.note(eid(7)) in written  # the new record note is still written


def test_rename_same_name_and_archive_move_keep_id_references(world: World) -> None:
    world.publisher.publish([P, K])
    # Archive move inside the AI folder: updated in place, never duplicated.
    archived = world.ai / "Archive" / "Projects" / f"{P}.md"
    archived.parent.mkdir(parents=True)
    world.note(P).rename(archived)
    # Rename and a different entity with the same display name.
    world.names.entities[1] = IndexedEntity(
        entity_id=K, type="person", name="김민수", aliases=["김 과장"]
    )
    world.names.entities.append(
        IndexedEntity(entity_id=X, type="person", name="김민수", aliases=[])
    )
    world.fabric.append([entry(7, entities=[P, K, X], value="세 사람 회의")])
    world.publisher.publish([P, K, X])
    assert list(world.ai.rglob(f"{P}.md")) == [archived]
    assert f"[[{K}|김민수]]" in archived.read_text()
    assert f"[[{X}|김민수]]" in archived.read_text()
    assert world.note(K) == world.ai / "Entities" / "People" / f"{K}.md"
    assert world.note(K) != world.note(X)
    assert '"김민수"' in frontmatter(world.note(K).read_text())
    # A human moving the PARA project folder to Archive breaks no AI link.
    (world.vault / "Archive").mkdir()
    (world.vault / "Projects" / "프로젝트 A").rename(world.vault / "Archive" / "A")
    assert world.publisher.publish([P, K, X]) == []


def test_failed_write_keeps_previous_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.publisher.publish([P])
    before = world.note(P).read_bytes()
    world.fabric.append([entry(7, field="status", value="진행")])

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        world.publisher.publish([P])
    assert world.note(P).read_bytes() == before
    assert not [p for p in world.ai.rglob(".*") if p.is_file()]


def test_paths_outside_ai_folder_are_rejected(world: World, tmp_path: Path) -> None:
    for bad in ("", "/abs", "../out", "a/../b", ".hidden", "a\\b"):
        with pytest.raises(PublishPathError):
            AiFolder(world.vault, bad)
    outside = tmp_path / "outside"
    outside.mkdir()
    (world.vault / "Weaver").symlink_to(outside)
    with pytest.raises(PublishPathError):
        world.publisher.publish([P])
    assert list(outside.iterdir()) == []
    with pytest.raises(ValueError):
        world.publisher.publish(["../../etc/passwd"])


def test_symlinked_note_is_not_followed(world: World, tmp_path: Path) -> None:
    target = tmp_path / "secret.md"
    target.write_text("밖의 파일\n")
    folder = world.ai / "Entities" / "People"
    folder.mkdir(parents=True)
    (folder / f"{K}.md").symlink_to(target)
    world.publisher.publish([K])
    assert target.read_text() == "밖의 파일\n"
    assert world.publisher.conflicts[0].reason == "not_regular_file"


def test_manifest_records_hash_and_ids_for_output_watcher(world: World) -> None:
    world.publisher.publish([P])
    outputs = world.state.outputs()
    path, digest = outputs[P]
    assert path == f"Weaver/Projects/{P}.md"
    assert embedded_hash((world.vault / path).read_bytes()) == digest
    assert outputs[eid(3)][0] == f"Weaver/Records/2026-11/{eid(3)}.md"


def test_vault_sync_publishes_changes_since_cursor(world: World) -> None:
    clock = [0.0]
    sync = VaultSync(
        world.publisher, world.fabric, world.names, world.state, clock=lambda: clock[0]
    )
    sync.publish()  # first call: full pass over registry + Fabric
    assert world.note(O).exists() and world.note(K).exists()
    calls: list[list[str]] = []
    original = world.publisher.publish

    def spy(changed: list[str]) -> list[Path]:
        calls.append(changed)
        return original(changed)

    world.publisher.publish = spy  # type: ignore[method-assign]
    clock[0] = 10.0
    sync.publish()
    assert calls[-1] == []
    world.fabric.append([entry(7, entities=[K], field="phone", value="010")])
    sync.publish()
    assert calls[-1] == [K]
    sync.publish()
    assert calls[-1] == []
    # A late append with an older recorded_at is caught by the periodic full pass.
    late = entry(8, entities=[O], recorded_at=datetime(2026, 1, 1, tzinfo=UTC))
    world.fabric.append([late])
    sync.publish()
    assert calls[-1] == []
    clock[0] += sync.full_interval
    sync.publish()
    assert set(calls[-1]) == {P, K, O}
    assert world.note(eid(8)).exists()


def test_review_relations_resolve_refs_and_this(tmp_path: Path) -> None:
    reviews = ReviewStore(tmp_path / "loom.sqlite3")
    proposal = json.loads((ROOT / "schema" / "examples" / "Proposal.json").read_text())
    proposal["relations"][0]["reason"] = "점검 담당"
    proposal["relations"].append(
        proposal["relations"][0] | {"from": "e2", "type": "후속조치", "to": "this"}
    )
    ids = {"e1": K, "e2": P}
    with reviews.connect() as db:
        for pid, verdict, result in (
            ("prp_1", "approve", [{"entry_id": eid(1)}]),
            ("prp_2", "reject", []),
        ):
            db.execute(
                "INSERT INTO reviews (proposal_id,rev,verdict,result,stage,approved,"
                "entity_ids) VALUES (?,1,?,?,'APPLIED',?,?)",
                (
                    pid,
                    json.dumps({"verdict": verdict}),
                    json.dumps(result),
                    json.dumps(proposal),
                    json.dumps(ids),
                ),
            )
    links = ReviewRelations(reviews).relations()
    source = proposal["source"]["source_id"]
    assert links == [
        RelationLink("소속", (K,), (P,), "점검 담당", source),
        RelationLink("후속조치", (P,), (eid(1),), "점검 담당", source),
    ]


def write_config(tmp_path: Path, extra: str) -> Path:
    roots = ("sync_root", "archive_root", "vault_root", "state_root", "log_root")
    config = tmp_path / "config.toml"
    config.write_text("\n".join(f'{k} = "{k}"' for k in roots) + "\n" + extra)
    for key in roots:
        (tmp_path / key).mkdir(exist_ok=True)
    for channel in ("outbox", "receipts", "proposals", "decisions"):
        (tmp_path / "sync_root" / channel).mkdir(exist_ok=True)
    return config


def test_config_switches_runtime_publisher(tmp_path: Path) -> None:
    off = load_config(write_config(tmp_path, ""), environ={})
    assert off.publish.enabled is False and off.publish.folder == "Weaver"
    fabric = FabricStore(tmp_path / "f.sqlite3")
    reviews = ReviewStore(tmp_path / "loom.sqlite3")
    assert vault_publisher(off, fabric, Names(), reviews) is None
    on = load_config(
        write_config(tmp_path, '[publish]\nenabled = true\nfolder = "AI/Weaver"\n'),
        environ={},
    )
    fabric.append(project_a())
    sync = vault_publisher(on, fabric, Names(), reviews)
    assert sync is not None
    sync.publish()
    assert (on.vault_root / "AI" / "Weaver" / "Projects" / f"{P}.md").exists()
    for bad in ('folder = "../x"', 'folder = ".obsidian"', "enabled = 1", "x = 1"):
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, f"[publish]\n{bad}\n"), environ={})


def test_same_input_same_bytes_across_stores(tmp_path: Path) -> None:
    outputs = []
    for name in ("a", "b"):
        world = World(tmp_path / name)
        world.publisher.publish([O, K, P])
        outputs.append(
            {k: v for k, v in world.files().items() if k.startswith("Weaver/")}
        )
    assert outputs[0] == outputs[1]
