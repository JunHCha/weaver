"""Synthetic internal bundle fixture; no claim about Webex REST response shape."""

import json
import sqlite3
from pathlib import Path

import pytest

from weaver.spinner import Spinner
from weaver.spinner.webex import WebexProvenanceStore, parse_webex

from .test_spinner import Store, source

FIXTURE = Path(__file__).parent / "fixtures/webex-bundle.json"


def test_bundle_sorts_preserves_unicode_offsets_and_keeps_private_ids():
    text = FIXTURE.read_text()
    script, provenance = parse_webex(source("webex", text), text)
    assert script.text.startswith(
        "[2026-11-19T01:00:00+00:00 · 가상 작성자] 합성 메시지 👋\n"
    )
    assert script.segments[0].speaker == "가상 작성자"
    assert script.segments[0].occurred_at.isoformat() == "2026-11-19T01:00:00+00:00"
    assert script.text[script.segments[0].start_char : script.segments[0].end_char] == (
        "[2026-11-19T01:00:00+00:00 · 가상 작성자] 합성 메시지 👋\n"
        "[2026-11-19T01:00:00+00:00 · 가상 작성자] 두 번째 줄"
    )
    assert (
        script.text[script.segments[1].start_char :]
        == "[2026-11-19T10:00:01+09:00 · 가상 동료] [첨부 1개 · 다운로드하지 않음]"
    )
    assert provenance.messages[1].parent_id == "SYNTHETIC_ROOT"
    assert provenance.messages[1].updated_at is not None
    for private in ("SYNTHETIC_", "fixture.invalid", "synthetic.pdf"):
        assert private not in script.model_dump_json()
    raw = json.loads(text)
    raw["messages"].reverse()
    assert parse_webex(source("webex", text), json.dumps(raw))[0] == script


def test_spinner_commits_private_resolver_before_success_and_replays(tmp_path):
    text = FIXTURE.read_text()
    src = source("webex", text)
    archive = Store(tmp_path, src, text)
    spinner = Spinner(archive, tmp_path, configuration_version="fixture")
    script = spinner.spin(src)
    assert spinner.was_revised(src) is False
    resolver = WebexProvenanceStore(tmp_path)
    assert resolver.resolve(src.source_id) == ("webex", "SYNTHETIC_PRIVATE_SPACE")
    assert resolver.resolve("unknown") is None
    assert resolver.path.stat().st_mode & 0o777 == 0o600
    assert (
        Spinner(archive, tmp_path, configuration_version="fixture").spin(src) == script
    )
    assert archive.calls == 1
    spinner.spin(src, reprocess=True)
    value = resolver.get(src.source_id)
    assert value is not None
    with pytest.raises(ValueError, match="webex_provenance_conflict"):
        resolver.save(value.model_copy(update={"space_id": "other"}))
    assert resolver.resolve(src.source_id) == ("webex", "SYNTHETIC_PRIVATE_SPACE")


@pytest.mark.parametrize(
    "mutation", ["unknown_format", "empty", "duplicate", "naive", "extra"]
)
def test_malformed_bundle_fails_without_leaking_raw_content(mutation):
    raw = json.loads(FIXTURE.read_text())
    if mutation == "unknown_format":
        raw["format"] = "future"
    elif mutation == "empty":
        raw["messages"] = []
    elif mutation == "duplicate":
        raw["messages"].append(raw["messages"][0])
    elif mutation == "naive":
        raw["messages"][0]["created_at"] = "2026-11-19T10:00:00"
    else:
        raw["secret"] = "SENSITIVE_INPUT"
    text = json.dumps(raw)
    with pytest.raises(ValueError, match="^webex_invalid_bundle$"):
        parse_webex(source("webex", text), text)


def test_wrong_channel_and_empty_message_rejected():
    text = FIXTURE.read_text()
    with pytest.raises(ValueError, match="webex_expected_text_source"):
        parse_webex(source("share_text", text), text)
    raw = json.loads(text)
    raw["messages"][0]["attachments"] = []
    with pytest.raises(ValueError, match="webex_invalid_bundle"):
        parse_webex(source("webex", text), json.dumps(raw))


def test_equal_instants_use_id_tie_break_and_offsets_sort_by_instant():
    raw = json.loads(FIXTURE.read_text())
    template = raw["messages"][1]
    raw["messages"] = [
        template | {"message_id": "b", "created_at": "2026-11-19T10:00:00+09:00"},
        template | {"message_id": "c", "created_at": "2026-11-18T20:30:00-04:00"},
        template | {"message_id": "a", "created_at": "2026-11-19T01:00:00Z"},
    ]
    text = json.dumps(raw)
    script, provenance = parse_webex(source("webex", text), text)
    assert [message.message_id for message in provenance.messages] == ["c", "a", "b"]
    assert [message.segment_id for message in provenance.messages] == ["s1", "s2", "s3"]
    for segment in script.segments:
        body = script.text[segment.start_char : segment.end_char]
        assert all("가상 작성자] " in line for line in body.splitlines())


def test_provenance_model_evolution_refreshes_defaults_and_mapping(tmp_path):
    text = FIXTURE.read_text()
    _, value = parse_webex(source("webex", text), text)
    store = WebexProvenanceStore(tmp_path)
    store.save(value)
    # Simulate an older model without the optional updated_at field/default.
    old = value.model_dump(mode="json")
    for message in old["messages"]:
        message.pop("updated_at")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE provenance SET body=?", (json.dumps(old),))
    store.save(value)
    store.save(value)
    assert store.get(value.source_id) == value
    stored = store.path.read_bytes()
    for private in ("synthetic.pdf", "fixture.invalid", "가상 작성자", "합성 메시지"):
        assert private.encode() not in stored
    assert value.format_version == 1
    for field in ("sha256", "instance_id", "space_id"):
        with pytest.raises(ValueError, match="webex_provenance_conflict"):
            store.save(value.model_copy(update={field: "changed"}))


def test_initial_unversioned_database_is_upgraded_without_content_copies(tmp_path):
    text = FIXTURE.read_text()
    raw = json.loads(text)
    src = source("webex", text)
    path = tmp_path / "webex-provenance.sqlite3"
    legacy = {
        "source_id": src.source_id,
        "sha256": src.sha256,
        "instance_id": raw["instance_id"],
        "space_id": raw["space_id"],
        "messages": raw["messages"],
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE provenance(source_id TEXT PRIMARY KEY,body TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO provenance VALUES(?,?)", (src.source_id, json.dumps(legacy))
        )
    store = WebexProvenanceStore(tmp_path)
    assert store.resolve(src.source_id) == ("webex", raw["space_id"])
    with sqlite3.connect(path) as db:
        body = json.loads(db.execute("SELECT body FROM provenance").fetchone()[0])
    assert body["format_version"] == 1
    assert set(body["messages"][0]) == {
        "message_id",
        "parent_id",
        "updated_at",
        "segment_id",
    }
    assert b"synthetic.pdf" not in path.read_bytes()


def test_webex_blocked_before_any_llm_path_and_title_has_no_unknown_counterpart():
    from weaver.llm.webex_guard import WebexLlmBlocked, require_webex_gate

    with pytest.raises(WebexLlmBlocked, match="webex_policy_gate_not_wired"):
        require_webex_gate("webex")
    require_webex_gate("call")


def test_webex_review_title_has_no_counterpart_or_channel_placeholder():
    from types import SimpleNamespace

    from weaver.runtime.summary import SummaryLookup

    text = FIXTURE.read_text()
    src = source("webex", text)
    lookup = SummaryLookup(Path("unused"), SimpleNamespace(get=lambda _: src))  # type: ignore[arg-type]
    title = lookup.title(src.source_id)
    assert title.endswith("Webex 대화")
    assert "알 수 없음" not in title


def test_processor_holds_webex_and_never_reaches_drafts(tmp_path):
    import threading
    from types import SimpleNamespace

    from weaver.runtime.summary import SummaryProcessor

    text = FIXTURE.read_text()
    src = source("webex", text)
    spun = []

    class Drafts:
        @property
        def state(self):
            raise AssertionError("loom must not be touched for webex")

    spinner = SimpleNamespace(spin=spun.append)
    processor = SummaryProcessor(spinner, Drafts(), None)  # type: ignore[arg-type]
    assert processor.process(src, threading.Event()) == "waiting_llm"
    assert spun == [src]


def test_loom_entry_points_fail_closed_for_webex():
    from weaver.llm.webex_guard import WebexLlmBlocked
    from weaver.loom.summary import SummaryDrafts

    drafts = SummaryDrafts.__new__(SummaryDrafts)
    with pytest.raises(WebexLlmBlocked):
        drafts.create("src", "text", source_type="webex")
