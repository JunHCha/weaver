"""Backlog scope, lineage rules and wiring of the central LLM policy (ADR-046)."""

import pytest

from weaver.config import load_config
from weaver.llm.ledger import Ledger
from weaver.llm.policy import (
    ALLOWED_CHANNELS,
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
        shared_sources=lambda: [UNKNOWN_DICTIONARY_SOURCE, UNKNOWN_WIKI_SOURCE],
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
    ContextDictionary(config.state_root).add_entry("합성 프로젝트", "project")
    if webex:
        archive_webex(config, 1, "SPACE_OPEN")
    return config, source.source_id, configured_policy(config)


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
    with provenance_scope([source_id], shared=True, requester=source_id):
        with pytest.raises(PolicyBlocked):  # dictionary lineage is unknown
            policy.prepare("s", "u")
    with provenance_scope([source_id]):
        assert policy.prepare("s", "u").user == "u"
