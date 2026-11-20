"""ADR-046 regressions: fake origins, transport and CLI only; no live services."""

import json
from dataclasses import replace

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from weaver.app import cli
from weaver.config import ConfigError, LlmConfig, load_config
from weaver.llm import create_client
from weaver.llm.cli import ClaudeCliClient
from weaver.llm.client import AnthropicClient
from weaver.llm.codex import CodexCliClient
from weaver.llm.errors import RateLimited
from weaver.llm.ledger import Ledger, MemoryLedger
from weaver.llm.policy import (
    LedgerBacklog,
    LlmPolicy,
    PolicyBlocked,
    ScopedClient,
    provenance_scope,
)
from weaver.llm.types import BackendReply

from .test_config import write_config


class Resolver:
    def __init__(self):
        self.origins = {
            "call": ("call", None),
            "webex": ("webex", "allowed-space"),
            "excluded": ("webex", "private-space"),
        }
        self.seen = []

    def resolve(self, source_id):
        self.seen.append(source_id)
        return self.origins.get(source_id)


def policy_for(resolver=None, **kwargs):
    return LlmPolicy(
        resolver or Resolver(),
        excluded_space_ids=frozenset({"private-space"}),
        **kwargs,
    )


@pytest.mark.parametrize("ids", [["excluded"], ["call", "excluded"], ["missing"], []])
def test_unknown_and_mixed_excluded_origins_fail_closed(ids):
    with provenance_scope(ids), pytest.raises(PolicyBlocked, match="^policy_blocked$"):
        policy_for().prepare("system", "secret-body")


def test_webex_default_open_but_requires_provenance_and_injected_exclusions():
    with provenance_scope(["webex"]):
        assert policy_for().prepare("s", "u").user == "u"
        with pytest.raises(PolicyBlocked):
            LlmPolicy(Resolver()).prepare("s", "u")
    with pytest.raises(PolicyBlocked):
        policy_for().prepare("s", "u")


def test_closed_channel_overrides_allowed_space():
    policy = policy_for()
    policy.settings = replace(
        policy.settings,
        channels=replace(
            policy.settings.channels,
            webex=replace(policy.settings.channels.webex, llm_allowed=False),
        ),
    )
    with provenance_scope(["webex"]), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")


def test_shared_wiki_prose_cannot_hide_a_denied_origin():
    policy = policy_for(shared_sources=lambda: ["call", "excluded"])
    with provenance_scope(["call"], shared=True), pytest.raises(PolicyBlocked):
        policy.prepare("s", "Derived Wiki prose without IDs")
    with provenance_scope(["call"], shared=True), pytest.raises(PolicyBlocked):
        policy_for().prepare("s", "Unknown shared lineage")


class Output(BaseModel):
    text: str


class Transport:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    async def send(self, request, api_key):
        self.calls.append(request)
        if self.callback:
            return self.callback()
        return BackendReply('{"text":"derived"}')


def api_client(tmp_path, policy, transport):
    return AnthropicClient(
        LlmConfig(api_key_env="FAKE_KEY"),
        Ledger(tmp_path / "llm.sqlite3"),
        policy=policy,
        transport=transport,
        environ={"FAKE_KEY": "fake"},
    )


@pytest.mark.parametrize("backend", ["anthropic", "claude_cli", "codex_cli"])
@pytest.mark.parametrize("structured", [False, True])
def test_all_external_backends_block_before_reservation_or_dispatch(
    tmp_path, backend, structured
):
    policy = policy_for()
    client = create_client(LlmConfig(backend), state_root=tmp_path, policy=policy)
    with provenance_scope(["excluded"]), pytest.raises(PolicyBlocked):
        if structured:
            client.generate_json("loom.summary", "s", "sensitive", Output)
        else:
            client.generate_text("wiki.compile", "s", "sensitive")
    assert client.ledger.records() == []
    assert not list(tmp_path.glob("*-workdir"))


@pytest.mark.parametrize("backend", ["anthropic", "claude_cli", "codex_cli"])
def test_direct_backend_constructors_fail_closed_without_policy(tmp_path, backend):
    ledger = Ledger(tmp_path / "llm.sqlite3")
    config = LlmConfig(backend)
    if backend == "anthropic":
        client = AnthropicClient(config, ledger)
    else:
        cls = ClaudeCliClient if backend == "claude_cli" else CodexCliClient
        client = cls(config, ledger, state_root=tmp_path)
    with pytest.raises(PolicyBlocked):
        client.generate_text("loom.summary", "s", "u")
    assert ledger.records() == []


def test_api_retry_rechecks_gate_with_captured_provenance_on_worker_thread(tmp_path):
    resolver = Resolver()

    def close_after_first():
        resolver.origins["webex"] = ("webex", "private-space")
        raise RateLimited()

    transport = Transport(close_after_first)
    client = api_client(tmp_path, policy_for(resolver), transport)
    with provenance_scope(["webex"]), pytest.raises(PolicyBlocked):
        client.generate_text("loom.summary", "s", "u")
    assert len(transport.calls) == 1
    assert client.ledger.records()[0]["attempts"] == 1


def test_masking_occurs_once_and_only_after_policy_check(tmp_path, monkeypatch):
    from weaver.llm import policy as module

    masked = []

    def mask(value):
        masked.append(value)
        return value + " MASKED"

    monkeypatch.setattr(module, "mask_patterns", mask)
    transport = Transport()
    client = api_client(tmp_path, policy_for(), transport)
    with provenance_scope(["excluded"]), pytest.raises(PolicyBlocked):
        client.generate_text("loom.summary", "s", "u")
    assert masked == []
    with provenance_scope(["call"]):
        client.generate_text("loom.summary", "name", "phone")
    assert masked == ["name", "phone"]
    assert (transport.calls[0].system, transport.calls[0].user) == (
        "name MASKED",
        "phone MASKED",
    )


def test_derived_text_and_nested_calls_retain_original_sources(tmp_path):
    resolver = Resolver()
    transport = Transport()
    client = api_client(tmp_path, policy_for(resolver), transport)
    original = ScopedClient(client, ["webex"])
    derived = original.generate_json("loom.summary", "s", "raw", Output)
    assert derived.text == "derived"
    # No raw IDs in the new text; caller carries the original Source lineage.
    wiki = ScopedClient(original, ["call"])
    wiki.generate_text("wiki.compile", "s", derived.text)
    assert {"webex", "call"} <= set(resolver.seen)
    resolver.origins["webex"] = ("webex", "private-space")
    with pytest.raises(PolicyBlocked):
        wiki.generate_text("loom.polish", "s", derived.text)
    assert len(transport.calls) == 2
    # Scope cleanup prevents accidental provenance reuse by unrelated callers.
    with pytest.raises(PolicyBlocked):
        client.generate_text("loom.summary", "s", "other")


def test_policy_reopening_and_restart_require_bounded_backlog_approval(tmp_path):
    ledger = Ledger(tmp_path / "llm.sqlite3")
    resolver = Resolver()
    policy = policy_for(resolver, backlog=LedgerBacklog(ledger))
    with provenance_scope(["excluded"]), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")
    reviewed = ledger.policy_backlog()
    resolver.origins["excluded"] = ("webex", "allowed-space")
    restarted = policy_for(resolver, backlog=LedgerBacklog(Ledger(ledger.path)))
    with provenance_scope(["excluded"]), pytest.raises(PolicyBlocked):
        restarted.prepare("s", "u")
    ledger.policy_block(frozenset({"webex"}))
    ledger.policy_resume(reviewed)
    assert [sid for sid, _ in ledger.policy_backlog()] == ["webex"]
    with provenance_scope(["excluded"]):
        restarted.prepare("s", "u")
    with provenance_scope(["webex"]), pytest.raises(PolicyBlocked):
        restarted.prepare("s", "u")
    with pytest.raises(ValueError, match="changed"):
        ledger.policy_resume(reviewed)


def test_dry_run_copies_policy_holds_without_persistent_changes(tmp_path):
    ledger = Ledger(tmp_path / "llm.sqlite3")
    ledger.policy_block(frozenset({"call"}))
    before = ledger.path.read_bytes()
    memory = MemoryLedger(ledger.path)
    policy = policy_for(backlog=LedgerBacklog(memory))
    with provenance_scope(["call"]), pytest.raises(PolicyBlocked):
        policy.prepare("s", "u")
    memory.policy_block(frozenset({"webex"}))
    assert ledger.path.read_bytes() == before
    assert [sid for sid, _ in ledger.policy_backlog()] == ["call"]


@pytest.mark.parametrize(
    "extra",
    [
        'llm_allowed = "secret"',
        "llm_allowed = 1",
        'policy_confirmed_at = "secret"',
        'unknown = "secret"',
    ],
)
def test_privacy_config_validation_is_redacted(tmp_path, extra):
    path = write_config(tmp_path, "[privacy.channels.webex]\n" + extra)
    with pytest.raises(ConfigError) as error:
        load_config(path, environ={})
    assert "secret" not in str(error.value)


def test_privacy_toml_defaults_and_close(tmp_path):
    path = write_config(tmp_path)
    assert load_config(path, environ={}).privacy.channels.webex.llm_allowed
    path.write_text(
        path.read_text() + "[privacy.channels.webex]\nllm_allowed = false\n"
    )
    assert not load_config(path, environ={}).privacy.channels.webex.llm_allowed


def test_backlog_cli_requires_explicit_scope_and_confirmation(tmp_path):
    path = write_config(tmp_path)
    state = tmp_path / "state_root"
    Ledger(state / "llm.sqlite3").policy_block(frozenset({"call"}))
    runner = CliRunner()
    shown = runner.invoke(cli, ["policy-backlog", "--config", str(path)])
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["count"] == 1
    for args in [["--resume", "--yes"], ["--resume", "--source-id", "call"]]:
        result = runner.invoke(cli, ["policy-backlog", "--config", str(path), *args])
        assert result.exit_code == 2
    # --yes approves a selection but cannot override unknown provenance.
    denied = runner.invoke(
        cli,
        [
            "policy-backlog",
            "--config",
            str(path),
            "--resume",
            "--source-id",
            "call",
            "--yes",
        ],
    )
    assert denied.exit_code == 2
    assert Ledger(state / "llm.sqlite3").policy_backlog()


class CheckedFake:
    """Exercise real caller lineage without provider parsing or process effects."""

    def __init__(self, fake, policy):
        self.fake = fake
        self.policy = policy
        self.inputs = []

    def generate_json(self, task, system, user, schema):
        self.inputs.append((task, self.policy.prepare(system, user).provenance))
        return self.fake.generate_json(task, system, user, schema)

    def generate_text(self, task, system, user):
        self.inputs.append((task, self.policy.prepare(system, user).provenance))
        return self.fake.generate_text(task, system, user)


def test_summary_polish_and_resummary_keep_source_lineage(tmp_path):
    from weaver.context import ContextDictionary
    from weaver.loom.summary import SummaryDrafts

    from .loom.test_summary_draft import FakeLlm

    source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    resolver = Resolver()
    resolver.origins[source_id] = ("webex", "allowed-space")
    checked = CheckedFake(FakeLlm(0), policy_for(resolver, shared_sources=lambda: ()))
    state = tmp_path / "state"
    drafts = SummaryDrafts(
        checked,
        ContextDictionary(state),
        tmp_path / "proposals",
        state_root=state,
        revision_threshold=1,
    )
    package, _ = drafts.create(source_id, "긴 원문", package_id="first")
    drafts.create(package.source_id, package.draft_body, package_id="second")
    assert [task for task, _ in checked.inputs] == [
        "loom.polish",
        "loom.summary",
        "loom.polish",
        "loom.summary",
    ]
    assert all(p.source_ids == frozenset({source_id}) for _, p in checked.inputs)
    resolver.origins[source_id] = ("webex", "private-space")
    with pytest.raises(PolicyBlocked):
        drafts.create(package.source_id, package.draft_body, package_id="closed")
    assert len(checked.inputs) == 4


def test_wiki_batch_and_retry_keep_thread_origins(tmp_path):
    from .wiki.test_batch import draft, setup

    _, fabric, fake, compiler = setup(tmp_path)
    source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    resolver = Resolver()
    resolver.origins[source_id] = ("webex", "allowed-space")
    checked = CheckedFake(fake, policy_for(resolver, shared_sources=lambda: ()))
    compiler.llm = checked
    compiler.run()
    assert checked.inputs[0][0] == "wiki.compile_batch"
    assert checked.inputs[0][1].source_ids == frozenset({source_id})
    resolver.origins[source_id] = ("webex", "private-space")
    fabric.append([draft("three", ["네트워크"])])
    for _ in range(2):
        with pytest.raises(PolicyBlocked):
            compiler.run()
    assert len(checked.inputs) == 1


def test_reextract_bootstrap_and_approval_keep_original_source(tmp_path):
    from weaver.context import ContextDictionary
    from weaver.loom.reextract import reextract

    from .loom.test_reextract import SOURCE, FakeLlm, seed

    state = tmp_path / "state"
    ContextDictionary(state)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    seed(state)
    resolver = Resolver()
    resolver.origins[SOURCE] = ("webex", "allowed-space")
    checked = CheckedFake(FakeLlm(), policy_for(resolver, shared_sources=lambda: ()))
    report = reextract(state, wiki, checked)
    assert report.valid
    assert [task for task, _ in checked.inputs] == [
        "loom.reextract_bootstrap",
        "loom.approve",
    ]
    assert all(p.source_ids == frozenset({SOURCE}) for _, p in checked.inputs)
    resolver.origins[SOURCE] = ("webex", "private-space")
    denied = reextract(state, wiki, checked)
    assert any("PolicyBlocked" in (plan.error or "") for plan in denied.sources)
    assert len(checked.inputs) == 2


def test_spinner_revision_keeps_source_lineage(tmp_path):
    from weaver.spinner.reviser import LlmReviser, ReviseHints

    from .spinner.test_reviser import SOURCE_ID, FakeLlm, make_thread

    resolver = Resolver()
    resolver.origins[SOURCE_ID] = ("webex", "private-space")
    checked = CheckedFake(
        FakeLlm(["문장"]), policy_for(resolver, shared_sources=lambda: ())
    )
    with pytest.raises(PolicyBlocked):
        LlmReviser(checked).revise(make_thread(["문장"]), ReviseHints())
    assert checked.inputs == []


def test_api_preview_rechecks_policy_on_worker_without_writing_ledger(tmp_path):
    ledger = Ledger(tmp_path / "llm.sqlite3")
    before = ledger.path.read_bytes()
    client = create_client(
        LlmConfig(api_key_env="FAKE_KEY"),
        state_root=tmp_path,
        dry_run=True,
        policy=policy_for(),
    )
    client.transport = Transport()
    client.environ = {"FAKE_KEY": "fake"}
    with provenance_scope(["call"]):
        client.generate_text("loom.summary", "s", "u")
    assert len(client.transport.calls) == 1
    assert ledger.path.read_bytes() == before


def test_source_resolver_failure_is_redacted_and_stays_closed():
    class Broken:
        def resolve(self, source_id):
            raise ValueError("private-space-and-message")

    with provenance_scope(["call"]), pytest.raises(PolicyBlocked) as error:
        policy_for(Broken()).prepare("s", "u")
    assert str(error.value) == "policy_blocked"


def test_invalid_privacy_never_initializes_directories(tmp_path):
    path = write_config(
        tmp_path, '[privacy.channels.webex]\nllm_allowed = "bad"', create=False
    )
    with pytest.raises(ConfigError):
        load_config(path, environ={}, init_directories=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_archive_adapter_checks_all_deduplicated_capture_origins(tmp_path):
    from weaver.archive import ArchiveStore
    from weaver.llm.policy_archive import ArchiveProvenanceResolver

    from .archive.conftest import CAP2, TEXT, manifest, outbox_file

    config = load_config(write_config(tmp_path), environ={})
    store = ArchiveStore.from_config(config)
    first = manifest(kind="text", channel="share_text")
    source = store.put(first, outbox_file(config.courier.outbox, first, TEXT))
    resolver = ArchiveProvenanceResolver(config.archive_root)
    assert resolver.resolve(source.source_id) == ("share_text", None)
    second = manifest(CAP2, kind="text", channel="kakao")
    assert store.put(second, outbox_file(config.courier.outbox, second, TEXT)) == source
    assert resolver.resolve(source.source_id) is None
    assert resolver.resolve("missing") is None
    assert resolver.source_ids() == (source.source_id,)


def test_backlog_cli_resumes_only_reviewed_known_sources(tmp_path):
    from weaver.archive import ArchiveStore

    from .archive.conftest import TEXT, manifest, outbox_file

    path = write_config(tmp_path)
    config = load_config(path, environ={})
    capture = manifest(kind="text", channel="share_text")
    source = ArchiveStore.from_config(config).put(
        capture, outbox_file(config.courier.outbox, capture, TEXT)
    )
    ledger = Ledger(config.state_root / "llm.sqlite3")
    ledger.policy_block(frozenset({source.source_id, "unknown"}))
    result = CliRunner().invoke(
        cli,
        [
            "policy-backlog",
            "--config",
            str(path),
            "--resume",
            "--source-id",
            source.source_id,
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.splitlines()[0])["count"] == 1
    assert json.loads(result.stdout.splitlines()[1]) == {"resumed": 1}
    assert [sid for sid, _ in ledger.policy_backlog()] == ["unknown"]


def test_unknown_wiki_page_provenance_is_denied_before_external_call(tmp_path):
    from .wiki.test_batch import setup

    store, _, fake, compiler = setup(tmp_path)
    source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    resolver = Resolver()
    resolver.origins[source_id] = ("webex", "allowed-space")
    checked = CheckedFake(
        fake,
        policy_for(resolver, shared_sources=lambda: (), webex_present=lambda: True),
    )
    compiler.llm = checked
    store.write("pages/Unknown.md", {}, "# Unknown\nPrivate derived prose", None)
    store.write_metadata("pages/Unknown.md", {"thread_ids": ["thr_gone"]})
    with pytest.raises(PolicyBlocked):
        compiler.run()
    assert checked.inputs == []


def test_closed_source_hold_is_recorded_even_without_backend_credentials(tmp_path):
    from types import SimpleNamespace

    from weaver.runtime.gate import LlmGate, LlmStatus
    from weaver.runtime.summary import SummaryProcessor

    ledger = Ledger(tmp_path / "llm.sqlite3")
    policy = policy_for(backlog=LedgerBacklog(ledger))
    processor = SummaryProcessor(
        None,
        None,
        LlmGate(lambda: LlmStatus(False, "authentication_error")),
        policy=policy,
    )
    with pytest.raises(PolicyBlocked):
        processor.process(
            SimpleNamespace(source_id="excluded", channel="share_text"), None
        )
    assert [sid for sid, _ in ledger.policy_backlog()] == ["excluded"]


def test_runtime_uses_one_policy_for_source_preflight_and_outbound_calls(tmp_path):
    from weaver.runtime.assemble import build_runtime

    config = load_config(write_config(tmp_path), environ={})
    runtime = build_runtime(config, environ={})
    policy = runtime.processor.policy
    assert policy is not None
    assert runtime.processor.drafts.llm.client.policy is policy
    assert isinstance(policy.backlog, LedgerBacklog)


def test_unprocessed_denied_capture_does_not_taint_unrelated_shared_input(tmp_path):
    from weaver.archive import ArchiveStore
    from weaver.llm.policy_archive import configured_policy

    from .archive.conftest import CAP2, TEXT, manifest, outbox_file

    config = load_config(write_config(tmp_path), environ={})
    archive = ArchiveStore.from_config(config)
    first = manifest(kind="text", channel="share_text")
    source = archive.put(first, outbox_file(config.courier.outbox, first, TEXT))
    second_body = TEXT + b" other"
    second = manifest(CAP2, body=second_body, kind="text", channel="share_text")
    excluded = archive.put(
        second, outbox_file(config.courier.outbox, second, second_body)
    )
    resolver = Resolver()
    resolver.origins[source.source_id] = ("call", None)
    resolver.origins[excluded.source_id] = ("webex", "private-space")
    policy = configured_policy(config)
    policy.resolver = resolver
    policy.excluded_space_ids = frozenset({"private-space"})
    with provenance_scope([source.source_id], shared=True):
        policy.prepare("s", "allowed content")
    assert excluded.source_id not in resolver.seen


def test_wiki_maintenance_blocks_then_resumes_through_the_real_policy(tmp_path):
    from .wiki.test_batch import setup

    _, _, fake, compiler = setup(tmp_path)
    source_id = "src_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    resolver = Resolver()
    resolver.origins[source_id] = ("webex", "private-space")
    ledger = Ledger(tmp_path / "llm.sqlite3")
    policy = policy_for(
        resolver, shared_sources=lambda: (), backlog=LedgerBacklog(ledger)
    )
    checked = CheckedFake(fake, policy)
    compiler.llm = checked
    with pytest.raises(PolicyBlocked):
        compiler.run()
    assert [sid for sid, _ in ledger.policy_backlog()] == [source_id]
    resolver.origins[source_id] = ("call", None)  # gate opens
    with pytest.raises(PolicyBlocked):
        compiler.run()  # still held until explicitly resumed
    ledger.policy_resume(ledger.policy_backlog())
    compiler.run()
    assert checked.inputs[0][0] == "wiki.compile_batch"


def test_loom_approval_blocks_then_resumes_through_the_real_policy(tmp_path):
    from weaver.context import ContextDictionary
    from weaver.loom.reextract import reextract

    from .loom.test_reextract import SOURCE, FakeLlm, seed

    state = tmp_path / "state"
    ContextDictionary(state)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    seed(state)
    resolver = Resolver()
    resolver.origins[SOURCE] = ("webex", "private-space")
    ledger = Ledger(tmp_path / "llm.sqlite3")
    checked = CheckedFake(
        FakeLlm(),
        policy_for(resolver, shared_sources=lambda: (), backlog=LedgerBacklog(ledger)),
    )
    denied = reextract(state, wiki, checked)
    assert any("PolicyBlocked" in (plan.error or "") for plan in denied.sources)
    assert checked.inputs == []
    assert [sid for sid, _ in ledger.policy_backlog()] == [SOURCE]
    resolver.origins[SOURCE] = ("call", None)
    ledger.policy_resume(ledger.policy_backlog())
    assert reextract(state, wiki, checked).valid
    assert "loom.approve" in [task for task, _ in checked.inputs]
