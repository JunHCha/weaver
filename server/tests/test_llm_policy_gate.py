"""Backlog scope, lineage rules and wiring of the central LLM policy (ADR-046)."""

import pytest

from weaver.config import load_config
from weaver.context import ContextDictionary
from weaver.llm.ledger import Ledger
from weaver.llm.policy import (
    ALLOWED_CHANNELS,
    HUMAN_DICTIONARY_SOURCE,
    UNKNOWN_DICTIONARY_SOURCE,
    UNKNOWN_WIKI_SOURCE,
    LedgerBacklog,
    LlmPolicy,
    PolicyBlocked,
    PolicyClient,
    Provenance,
    provenance_scope,
)

from .test_config import write_config


class Resolver:
    def __init__(self, origins=None):
        self.origins = origins or {}

    def resolve(self, source_id):
        return self.origins.get(source_id, ("call", None))


def make_policy(tmp_path, resolver=None, **kwargs):
    ledger = Ledger(tmp_path / "llm.sqlite3")
    policy = LlmPolicy(
        resolver or Resolver({"closed": ("webex", "private")}),
        excluded_space_ids=frozenset({"private"}),
        backlog=LedgerBacklog(ledger),
        **kwargs,
    )
    return policy, ledger


def test_allowed_channels_come_from_the_channel_type():
    from typing import get_args

    from weaver.schema.models import Channel

    assert ALLOWED_CHANNELS == set(get_args(Channel.__value__)) - {"webex"}


def test_backlog_holds_only_closed_origins_and_the_requester_and_resumes(tmp_path):
    open_ids = [f"open{i}" for i in range(300)]
    policy, ledger = make_policy(
        tmp_path,
        shared_sources=lambda: [*open_ids, "closed"],
        webex_present=lambda: True,
    )
    with provenance_scope(["req"], shared=True, requester="req"):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")
    held = [sid for sid, _ in ledger.policy_backlog()]
    assert held == ["closed", "req"]  # no open Source is dragged in
    # Shared work stays blocked while the origin is closed, and is not
    # deadlocked by the backlog once the origin opens and is resumed.
    policy.resolver = Resolver()  # "closed" is now open
    with provenance_scope(["req"], shared=True, requester="req"):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")  # still pending until resumed
        ledger.policy_resume(ledger.policy_backlog())
        assert policy.prepare("s", "u").user == "u"
    assert ledger.policy_backlog() == []


def test_sentinel_ids_are_never_recorded(tmp_path):
    policy, ledger = make_policy(
        tmp_path,
        shared_sources=lambda: [UNKNOWN_WIKI_SOURCE, UNKNOWN_DICTIONARY_SOURCE],
        webex_present=lambda: True,
    )
    with provenance_scope([], shared=True):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")
    assert ledger.policy_backlog() == []


def test_transient_faults_block_the_call_without_a_backlog_record(tmp_path):
    class Flaky(Resolver):
        broken = True

        def resolve(self, source_id):
            if self.broken:
                raise OSError("locked")
            return super().resolve(source_id)

    resolver = Flaky()
    policy, ledger = make_policy(tmp_path, resolver)
    with provenance_scope(["req"], requester="req"):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")
        resolver.broken = False
        assert policy.prepare("s", "u").user == "u"  # evaluated again, not held
    assert ledger.policy_backlog() == []

    def failing():
        raise OSError("db locked")

    policy.shared_sources = failing
    with provenance_scope(["req"], shared=True, requester="req"):
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")
    assert ledger.policy_backlog() == []


def test_unknown_lineage_is_allowed_only_while_no_webex_source_exists(tmp_path):
    present = [False]
    policy, ledger = make_policy(
        tmp_path,
        shared_sources=lambda: [UNKNOWN_WIKI_SOURCE],
        webex_present=lambda: present[0],
    )
    with provenance_scope(["req"], shared=True, requester="req"):
        assert policy.prepare("s", "u").user == "u"
        present[0] = True
        with pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")
    # Without the archive probe, unknown lineage always fails closed.
    policy.webex_present = None
    present[0] = False
    with provenance_scope(["req"], shared=True), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")
    assert [sid for sid, _ in ledger.policy_backlog()] == ["req"]


def test_check_without_requester_records_only_closed_origin(tmp_path):
    policy, ledger = make_policy(tmp_path)
    with pytest.raises(PolicyBlocked):
        policy.check(Provenance(frozenset({"closed", "other"})))
    assert [sid for sid, _ in ledger.policy_backlog()] == ["closed"]


def test_injected_client_still_goes_through_the_policy(tmp_path):
    from weaver.runtime.assemble import Plugins, build_runtime

    class Fake:
        def generate_json(self, task, system, user, schema):
            raise AssertionError("must not be reached")

        def generate_text(self, task, system, user):
            raise AssertionError("must not be reached")

    config = load_config(write_config(tmp_path), environ={})
    runtime = build_runtime(config, plugins=Plugins(client=Fake()), environ={})
    with pytest.raises(PolicyBlocked):  # unknown Source: not in the archive
        runtime.processor.drafts.create("src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "text")
    client = PolicyClient(Fake(), LlmPolicy())  # type: ignore[arg-type]
    with pytest.raises(PolicyBlocked):
        client.generate_text("t", "s", "u")


# ---- wiring of the Webex resolver and exclusions (#191, #194) ----------------


def archive_webex(config, number, space_id, *, provenance=True):
    from weaver.archive import ArchiveStore
    from weaver.spinner.webex import WebexProvenance, WebexProvenanceStore

    from .archive.conftest import manifest, outbox_file

    body = f"synthetic webex {number}".encode()
    m = manifest(
        f"cap_01ARZ3NDEKTSV4RRFFQ69G5FA{number}", body, kind="text", channel="webex"
    )
    source = ArchiveStore.from_config(config).put(
        m, outbox_file(config.courier.outbox, m, body)
    )
    if provenance:
        WebexProvenanceStore(config.state_root).save(
            WebexProvenance(
                source_id=source.source_id,
                sha256=source.sha256,
                instance_id="synthetic-instance",
                space_id=space_id,
                messages=[],
            )
        )
    return source.source_id


def test_configured_exclusions_block_matching_webex_spaces(tmp_path):
    from weaver.llm.policy_archive import configured_policy

    extra = '[shepherds.webex]\nexclude_space_ids = ["SPACE_PRIVATE"]\n'
    config = load_config(write_config(tmp_path, extra), environ={})
    assert config.shepherds.webex.exclude_space_ids == ("SPACE_PRIVATE",)
    private = archive_webex(config, 0, "SPACE_PRIVATE")
    shared = archive_webex(config, 1, "SPACE_OPEN")
    orphan = archive_webex(config, 2, "SPACE_OPEN", provenance=False)
    policy = configured_policy(config)
    with provenance_scope([shared]):
        assert policy.prepare("s", "u").user == "u"
    for blocked in (private, orphan):  # excluded Space; no provenance at all
        with provenance_scope([blocked]), pytest.raises(PolicyBlocked):
            policy.prepare("s", "u")


def test_missing_section_means_no_exclusions_but_still_fails_closed(tmp_path):
    from weaver.llm.policy_archive import configured_policy

    config = load_config(write_config(tmp_path), environ={})
    assert config.shepherds.webex.enabled is False
    assert config.shepherds.webex.exclude_space_ids == ()
    known = archive_webex(config, 0, "ANY_SPACE")
    unknown = archive_webex(config, 1, "ANY_SPACE", provenance=False)
    policy = configured_policy(config)
    assert policy.excluded_space_ids == frozenset()
    with provenance_scope([known]):
        assert policy.prepare("s", "u").user == "u"
    with provenance_scope([unknown]), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")


def test_processor_resolves_webex_after_local_parse_and_holds_until_wired(tmp_path):
    import threading
    from types import SimpleNamespace

    from weaver.llm.policy_archive import configured_policy
    from weaver.runtime.summary import SummaryProcessor

    config = load_config(
        write_config(tmp_path, '[shepherds.webex]\nexclude_space_ids = ["X"]\n'),
        environ={},
    )
    source_id = archive_webex(config, 0, "X", provenance=False)
    spun = []

    def spin(source):
        spun.append(source.source_id)
        from weaver.spinner.webex import WebexProvenance, WebexProvenanceStore

        WebexProvenanceStore(config.state_root).save(
            WebexProvenance(
                source_id=source_id,
                sha256=source.sha256,
                instance_id="i",
                space_id="X",
                messages=[],
            )
        )

    from weaver.archive import ArchiveStore

    source = ArchiveStore.from_config(config).get(source_id)
    ledger = Ledger(config.state_root / "llm.sqlite3")
    policy = configured_policy(config)
    policy.backlog = LedgerBacklog(ledger)
    processor = SummaryProcessor(
        SimpleNamespace(spin=spin),
        None,
        None,
        None,
        policy=policy,  # type: ignore[arg-type]
    )
    with pytest.raises(PolicyBlocked):
        processor.process(source, threading.Event())
    assert spun == [source_id]
    assert [sid for sid, _ in ledger.policy_backlog()] == [source_id]


# ---- Wiki / dictionary lineage ------------------------------------------------


@pytest.fixture
def wiki_store(tmp_path):
    from weaver.wiki.store import WikiStore

    return WikiStore(tmp_path / "vault", tmp_path / "state", settle_seconds=0)


def test_wiki_lineage_rules(wiki_store):
    from weaver.wiki.provenance import source_ids

    store = wiki_store
    store.write("pages/Human.md", {}, "# human", None)  # no ingested origin
    store.write("pages/deep/er/Sub.md", {}, "# sub", None)
    store.write_metadata("pages/deep/er/Sub.md", {"source_ids": ["src_direct"]})
    store.write("pages/Legacy.md", {}, "# legacy", None)
    store.write_metadata("pages/Legacy.md", {"thread_ids": ["t1"]})
    threads = {"t1": "src_from_thread"}
    assert source_ids(store, threads) == {"src_direct", "src_from_thread"}
    store.write("pages/Gone.md", {}, "# gone", None)
    store.write_metadata("pages/Gone.md", {"thread_ids": ["t_missing"]})
    assert source_ids(store, threads) == {
        "src_direct",
        "src_from_thread",
        UNKNOWN_WIKI_SOURCE,
    }


def test_human_only_wiki_has_no_origins_and_broken_meta_is_unknown(
    wiki_store, monkeypatch
):
    from weaver.wiki.provenance import source_ids

    wiki_store.write("pages/Human.md", {}, "# human", None)
    assert source_ids(wiki_store, {}) == set()

    def broken(path):
        raise ValueError("corrupt")

    monkeypatch.setattr(wiki_store, "metadata", broken)
    assert source_ids(wiki_store, {}) == {UNKNOWN_WIKI_SOURCE}


def test_reextract_survives_broken_wiki_metadata(tmp_path, monkeypatch):
    from weaver.context import ContextDictionary
    from weaver.loom.reextract import reextract
    from weaver.wiki.store import WikiStore

    from .loom.test_reextract import SOURCE, FakeLlm, seed

    state = tmp_path / "state"
    ContextDictionary(state)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    seed(state)

    def explode(*args, **kwargs):
        raise OSError("broken wiki")

    monkeypatch.setattr(WikiStore, "open_readonly", explode)
    seen = []

    class Seen(FakeLlm):
        def generate_json(self, task, system, user, schema):
            from weaver.llm.policy import _current

            seen.append(_current.get())
            return super().generate_json(task, system, user, schema)

    report = reextract(state, wiki, Seen())
    assert report.valid
    assert all(UNKNOWN_WIKI_SOURCE in p.source_ids for p in seen if p)
    assert SOURCE


def make_vault(tmp_path, *, webex):
    """A configured policy over a real archive; non-Webex unless asked."""
    from weaver.archive import ArchiveStore
    from weaver.context import ContextDictionary
    from weaver.llm.policy_archive import configured_policy

    from .archive.conftest import manifest, outbox_file

    config = load_config(write_config(tmp_path), environ={})
    body = b"synthetic share text"
    m = manifest(body=body, kind="text", channel="share_text")
    source = ArchiveStore.from_config(config).put(
        m, outbox_file(config.courier.outbox, m, body)
    )
    ContextDictionary(config.state_root, archive_root=config.archive_root).add_entry(
        "합성 프로젝트", "project"
    )
    if webex:
        archive_webex(config, 1, "SPACE_OPEN")
    return config, source.source_id, configured_policy(config)


@pytest.fixture(params=[False, True], ids=["live", "snapshot"])
def fabric_policy(tmp_path, request):
    from weaver.fabric import FabricStore
    from weaver.llm.policy_archive import configured_policy

    from .spec.test_facts_and_history import draft

    config, source_id, _ = make_vault(tmp_path, webex=True)
    other = archive_webex(config, 2, "SPACE_OPEN")
    fabric = FabricStore.from_config(config)
    fabric.append(
        [
            draft("one", source_id=source_id),
            draft("two", source_id=other),
            draft("three", source_id=source_id),
        ]
    )
    return (
        config,
        fabric,
        source_id,
        other,
        configured_policy(config, snapshot=request.param),
    )


def test_shared_sources_reads_fabric_draft_payloads(fabric_policy):
    _, _, source_id, other, policy = fabric_policy
    assert policy.shared_sources() == tuple(sorted({source_id, other}))


def test_shared_call_passes_with_fabric_draft_payloads(fabric_policy):
    _, _, _, _, policy = fabric_policy

    class Fake:
        def generate_text(self, task, system, user):
            assert (task, system, user) == ("wiki", "s", "u")
            return "synthetic response"

    client = PolicyClient(Fake(), policy)
    # Wiki maintenance may have no requester and rely entirely on shared lineage.
    with provenance_scope([], shared=True):
        assert client.generate_text("wiki", "s", "u") == "synthetic response"


def test_shared_fabric_sources_still_enforce_exclusions(fabric_policy):
    from .spec.test_facts_and_history import draft

    config, fabric, source_id, _, policy = fabric_policy
    excluded = archive_webex(config, 3, "SPACE_PRIVATE")
    fabric.append([draft("private", source_id=excluded)])
    policy.excluded_space_ids = frozenset({"SPACE_PRIVATE"})
    with provenance_scope([source_id], shared=True), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")


def test_non_webex_vault_first_summary_and_wiki_are_not_blocked(tmp_path):
    config, source_id, policy = make_vault(tmp_path, webex=False)
    # A dictionary with entries but no approved lineage yet: first summary.
    with provenance_scope([source_id], shared=True, requester=source_id):
        assert policy.prepare("s", "u").user == "u"
    # Wiki maintenance over human-only pages with no Fabric Threads at all.
    with provenance_scope([], shared=True):
        assert policy.prepare("s", "u").user == "u"


def test_webex_in_archive_blocks_unknown_lineage_but_not_known_open_input(tmp_path):
    config, source_id, policy = make_vault(tmp_path, webex=True)
    from weaver.context import ContextDictionary

    ContextDictionary(config.state_root, archive_root=config.archive_root).add_entry(
        "After Webex", "topic"
    )
    with provenance_scope([source_id], shared=True, requester=source_id):
        with pytest.raises(PolicyBlocked):  # dictionary lineage is unknown
            policy.prepare("s", "u")
    with provenance_scope([source_id]):
        assert policy.prepare("s", "u").user == "u"


def test_dictionary_created_before_first_webex_does_not_block_first_summary(tmp_path):
    from weaver.context import ContextDictionary
    from weaver.llm.policy_archive import configured_policy

    config = load_config(write_config(tmp_path), environ={})
    ContextDictionary(config.state_root, archive_root=config.archive_root).add_entry(
        "Earlier", "topic"
    )
    source = archive_webex(config, 0, "OPEN")
    with provenance_scope([source], shared=True):
        assert configured_policy(config).prepare("s", "u").user == "u"


@pytest.mark.parametrize("snapshot", [False, True])
def test_dictionary_sources_are_checked_even_with_existing_approved_context(
    tmp_path, snapshot
):
    from weaver.context import ContextDictionary
    from weaver.fabric import FabricStore
    from weaver.llm.policy_archive import configured_policy

    from .spec.test_facts_and_history import draft

    config = load_config(write_config(tmp_path), environ={})
    open_id = archive_webex(config, 0, "OPEN")
    closed_id = archive_webex(config, 1, "PRIVATE")
    FabricStore(config.state_root / "fabric.sqlite3").append(
        [draft("open", source_id=open_id)]
    )
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry("Private-derived", "topic", source_ids=(closed_id,))
    policy = configured_policy(config, snapshot=snapshot)
    policy.excluded_space_ids = frozenset({"PRIVATE"})
    assert closed_id in policy.shared_sources()
    with provenance_scope([open_id], shared=True), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")


def test_dictionary_known_open_source_allows_first_summary_and_unknown_stays_closed(
    tmp_path,
):
    from weaver.context import ContextDictionary
    from weaver.llm.policy_archive import configured_policy

    config = load_config(write_config(tmp_path), environ={})
    source = archive_webex(config, 0, "OPEN")
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry("Known", "topic", source_ids=(source,))
    with provenance_scope([source], shared=True):
        assert configured_policy(config).prepare("s", "u").user == "u"
    dictionary.add_entry("Unknown", "topic")
    with provenance_scope([source], shared=True), pytest.raises(PolicyBlocked):
        configured_policy(config).prepare("s", "u")


def test_dictionary_write_time_flag_survives_clock_rollback_and_receipt_removal(
    tmp_path,
):
    import sqlite3

    from weaver.context import ContextDictionary
    from weaver.llm.policy_archive import configured_policy

    config = load_config(write_config(tmp_path), environ={})
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry("Earlier", "topic")
    dictionary.add_entry(
        "Earlier unknown wiki", "topic", source_ids=(UNKNOWN_WIKI_SOURCE,)
    )
    source = archive_webex(config, 0, "OPEN")
    # The archived clock goes backwards; there is no time comparison to change policy.
    with sqlite3.connect(config.archive_root / "index.sqlite3") as db:
        db.execute(
            "UPDATE captures SET receipt_json=json_set(receipt_json, '$.received_at', '1900-01-01T00:00:00Z')"
        )
    with provenance_scope([source], shared=True):
        assert configured_policy(config).prepare("s", "u").user == "u"
    dictionary.add_entry("Human after Webex", "topic")
    with sqlite3.connect(config.archive_root / "index.sqlite3") as db:
        db.execute("DELETE FROM captures")
        db.execute("DELETE FROM sources")
    assert dictionary.policy_origins()[1]
    with provenance_scope([], shared=True), pytest.raises(PolicyBlocked):
        configured_policy(config).prepare("s", "u")


def test_dictionary_legacy_migration_uses_archive_presence(tmp_path):
    import sqlite3

    from weaver.context import ContextDictionary

    config = load_config(write_config(tmp_path), environ={})
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry("Legacy", "topic")
    with sqlite3.connect(config.state_root / "context.sqlite3") as db:
        db.execute("DROP TABLE entry_origins")
    archive_webex(config, 0, "OPEN")
    migrated = ContextDictionary(config.state_root, archive_root=config.archive_root)
    assert migrated.policy_origins()[1]


def test_web_human_edits_after_webex_do_not_close_first_webex_summary(tmp_path):
    from weaver.llm.policy_archive import configured_policy
    from weaver.web.editing import (
        AliasEdit,
        EntryEdit,
        MergeEdit,
        add_alias,
        edit_entry,
        merge,
        remove_alias,
    )

    config, _, _ = make_vault(tmp_path, webex=True)
    add_alias(config, AliasEdit(entry="합성 프로젝트", alias="human alias"))
    remove_alias(config, AliasEdit(entry="합성 프로젝트", alias="human alias"))
    edit_entry(config, EntryEdit(entry="합성 프로젝트", name="사람 이름"))
    edit_entry(config, EntryEdit(entry="사람 이름", kind="topic"))
    ContextDictionary(config.state_root, archive_root=config.archive_root).add_entry(
        "병합 대상", "topic", source_ids=(HUMAN_DICTIONARY_SOURCE,)
    )
    merge(config, MergeEdit(entry="사람 이름", target="병합 대상"))
    dictionary = ContextDictionary(config.state_root)
    ids, unknown = dictionary.policy_origins()
    assert HUMAN_DICTIONARY_SOURCE in ids and not unknown
    fresh = archive_webex(config, 5, "SPACE_OPEN")
    with provenance_scope([fresh], shared=True):
        assert configured_policy(config).prepare("s", "u").user == "u"


def test_automatic_unknown_write_after_webex_stays_closed_with_human_edit(tmp_path):
    from weaver.llm.policy_archive import configured_policy
    from weaver.web.editing import AliasEdit, add_alias

    config, _, _ = make_vault(tmp_path, webex=True)
    add_alias(config, AliasEdit(entry="합성 프로젝트", alias="human alias"))
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry("Auto unknown", "topic", human_edited=False)
    assert dictionary.policy_origins()[1]
    with provenance_scope([], shared=True), pytest.raises(PolicyBlocked):
        configured_policy(config).prepare("s", "u")
    # A later human edit cannot launder an already unknown entry.
    add_alias(config, AliasEdit(entry="Auto unknown", alias="typed alias"))
    with provenance_scope([], shared=True), pytest.raises(PolicyBlocked):
        configured_policy(config).prepare("s", "u")


def test_human_edit_mixed_with_excluded_webex_space_stays_closed(tmp_path):
    from weaver.llm.policy_archive import configured_policy
    from weaver.web.editing import AliasEdit, add_alias

    extra = '[shepherds.webex]\nexclude_space_ids = ["SPACE_PRIVATE"]\n'
    config = load_config(write_config(tmp_path, extra), environ={})
    private = archive_webex(config, 0, "SPACE_PRIVATE")
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry("Private derived", "topic", source_ids=(private,))
    add_alias(config, AliasEdit(entry="Private derived", alias="typed alias"))
    ids, unknown = dictionary.policy_origins()
    assert {private, HUMAN_DICTIONARY_SOURCE} <= ids and not unknown
    with provenance_scope([], shared=True), pytest.raises(PolicyBlocked):
        configured_policy(config).prepare("s", "u")


def test_non_webex_promotion_and_reextract_alias_do_not_poison_first_webex_summary(
    tmp_path,
):
    from datetime import timedelta

    from weaver.context import ContextDictionary
    from weaver.fabric import FabricStore, ThreadQuery
    from weaver.llm.policy_archive import configured_policy
    from weaver.loom.reextract import NameChange, SourcePlan, _apply
    from weaver.loom.tagging import NewEntry
    from weaver.wiki.promotion import ProjectLifecycle

    from .wiki.test_promotion import AREA, NAME, NOW, fact, proposal

    config, origin, _ = make_vault(tmp_path, webex=False)
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry(AREA, "area", source_ids=(origin,))
    dictionary.add_entry(
        "Before rename", "topic", human_edited=False, source_ids=(origin,)
    )
    archive_webex(config, 1, "OPEN")
    fabric = FabricStore.from_config(config)
    fabric.append([fact("one", day=5, source_id=origin), fact("two", source_id=origin)])
    lifecycle = ProjectLifecycle(
        config.state_root / "promotion.sqlite3", dictionary, fabric
    )
    lifecycle.apply(
        [proposal()], fabric.entries(ThreadQuery()), NOW + timedelta(days=1)
    )
    assert dictionary.resolve(NAME).promoted
    _apply(
        config.state_root,
        fabric,
        SourcePlan(
            source_id=origin,
            key="synthetic",
            renames=[NameChange(observed="Before rename", canonical="Renamed")],
            additions=[NewEntry(name="Renamed", kind="topic", aliases=["new alias"])],
        ),
    )
    assert dictionary.resolve("new alias").name == "Renamed"
    source = archive_webex(config, 2, "OPEN")
    with provenance_scope([source], shared=True):
        assert configured_policy(config).prepare("s", "u").user == "u"


def test_compiler_dictionary_pages_include_page_and_section_sources(tmp_path):
    from weaver.context import ContextDictionary
    from weaver.fabric import FabricStore
    from weaver.llm.policy_archive import configured_policy
    from weaver.wiki.compiler import Compiler, Summary
    from weaver.wiki.store import WikiStore

    from .wiki.test_compiler import FakeLLM

    config, origin, _ = make_vault(tmp_path, webex=True)
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    store = WikiStore(config.vault_root, config.state_root / "wiki")
    store.write("pages/Known.md", {"kind": "topic"}, "# Known", None)
    store.write_metadata(
        "pages/Known.md",
        {"source_ids": [origin], "sections": {"s": {"source_ids": [origin]}}},
    )
    compiler = Compiler(
        store,
        FabricStore.from_config(config),
        dictionary,
        FakeLLM(),
        config.state_root / "compiler.sqlite3",
        lambda _: Summary("s", "u"),
    )
    compiler._sync_pages()
    source = archive_webex(config, 2, "OPEN")
    with provenance_scope([source], shared=True):
        assert configured_policy(config).prepare("s", "u").user == "u"


def _private_vault(tmp_path):
    extra = '[shepherds.webex]\nexclude_space_ids = ["SPACE_PRIVATE"]\n'
    config = load_config(write_config(tmp_path, extra), environ={})
    private = archive_webex(config, 0, "SPACE_PRIVATE")
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    return config, private, dictionary


def _assert_blocked(config):
    from weaver.llm.policy_archive import configured_policy

    with provenance_scope([], shared=True), pytest.raises(PolicyBlocked):
        configured_policy(config).prepare("s", "u")


@pytest.mark.parametrize("dirty_is_source", [True, False])
def test_merge_keeps_excluded_webex_origin_from_either_side(tmp_path, dirty_is_source):
    from weaver.web.editing import MergeEdit, merge

    config, private, dictionary = _private_vault(tmp_path)
    dictionary.add_entry("Private derived", "topic", source_ids=(private,))
    dictionary.add_entry("Clean human", "topic", source_ids=(HUMAN_DICTIONARY_SOURCE,))
    entry, target = (
        ("Private derived", "Clean human")
        if dirty_is_source
        else ("Clean human", "Private derived")
    )
    merge(config, MergeEdit(entry=entry, target=target))
    ids, _ = dictionary.policy_origins()
    assert {private, HUMAN_DICTIONARY_SOURCE} <= ids
    _assert_blocked(config)


@pytest.mark.parametrize("unknown_is_source", [True, False])
def test_merge_keeps_unknown_flag_from_either_side(tmp_path, unknown_is_source):
    from weaver.web.editing import MergeEdit, merge

    config, _, _ = make_vault(tmp_path, webex=True)
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry("Auto unknown", "topic", human_edited=False)
    dictionary.add_entry("Clean human", "topic", source_ids=(HUMAN_DICTIONARY_SOURCE,))
    entry, target = (
        ("Auto unknown", "Clean human")
        if unknown_is_source
        else ("Clean human", "Auto unknown")
    )
    merge(config, MergeEdit(entry=entry, target=target))
    assert dictionary.policy_origins()[1]
    _assert_blocked(config)


def test_rename_keeps_excluded_webex_origin(tmp_path):
    from weaver.web.editing import EntryEdit, edit_entry

    config, private, dictionary = _private_vault(tmp_path)
    dictionary.add_entry("Private derived", "topic", source_ids=(private,))
    edit_entry(config, EntryEdit(entry="Private derived", name="Renamed"))
    assert private in dictionary.policy_origins()[0]
    _assert_blocked(config)


def test_automatic_paths_cannot_claim_the_human_source(tmp_path):
    config, _, _ = make_vault(tmp_path, webex=False)
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    human = (HUMAN_DICTIONARY_SOURCE,)
    with pytest.raises(ValueError):
        dictionary.add_entry("X", "topic", human_edited=False, source_ids=human)
    with pytest.raises(ValueError):
        dictionary.learn("obs", "합성 프로젝트", "project", source_ids=human)
    with pytest.raises(ValueError):
        dictionary.rename("합성 프로젝트", "Y", human_edited=False, source_ids=human)
    with pytest.raises(ValueError):
        dictionary.add_alias("합성 프로젝트", "z", human_edited=False, source_ids=human)


def test_existing_entry_source_attach_does_not_add_human(tmp_path):
    config, _, _ = make_vault(tmp_path, webex=False)
    dictionary = ContextDictionary(config.state_root, archive_root=config.archive_root)
    dictionary.add_entry(
        "합성 프로젝트", "project", source_ids=(HUMAN_DICTIONARY_SOURCE, "src-1")
    )
    ids, _ = dictionary.policy_origins()
    assert "src-1" in ids and HUMAN_DICTIONARY_SOURCE not in ids
