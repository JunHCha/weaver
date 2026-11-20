"""Synthetic internal bundle fixture; no claim about Webex REST response shape."""

import json
from pathlib import Path

import pytest

from weaver.spinner import Spinner
from weaver.spinner.webex import WebexProvenanceStore, parse_webex

from .test_spinner import Store, source

FIXTURE = Path(__file__).parent / "fixtures/webex-bundle.json"


def test_bundle_sorts_preserves_unicode_offsets_and_keeps_private_ids():
    text = FIXTURE.read_text()
    script, provenance = parse_webex(source("webex", text), text)
    assert script.text.startswith("합성 메시지 👋\n두 번째 줄\n")
    assert script.segments[0].speaker == "가상 작성자"
    assert script.segments[0].occurred_at.isoformat() == "2026-11-19T01:00:00+00:00"
    assert script.text[script.segments[0].start_char : script.segments[0].end_char] == (
        "합성 메시지 👋\n두 번째 줄"
    )
    assert (
        script.text[script.segments[1].start_char :] == "[첨부 1개 · 다운로드하지 않음]"
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
