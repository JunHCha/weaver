"""Synthetic archived text only; no live LLM, speech runtime or user vault."""

import hashlib
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

import pytest

from weaver.config import ConfigError, SpinnerSettings
from weaver.llm import RateLimited
from weaver.schema.internal import ArchivedSource
from weaver.schema.models import Source
from weaver.spinner import Spinner, SpinnerError, SpinnerTransientError
from weaver.spinner.reviser import Revision, RevisionSegment, serialize_revision
from weaver.spinner.text import TextSpinner, text_to_thread

TEXT = "  합성 텍스트\r\n다음 줄\n "


def source(channel="share_text", text=TEXT, **updates):
    raw = json.loads(
        (Path(__file__).parents[3] / "schema/examples/Source.json").read_text()
    )
    raw.update(
        kind="text", channel=channel, sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    raw.update(updates)
    return Source.model_validate(raw)


class Store:
    def __init__(self, root, src, text=TEXT):
        self.source = src
        self.path = root / "payload.txt"
        self.path.write_bytes(text.encode())
        self.calls = 0

    def locate(self, source_id):
        assert source_id == self.source.source_id
        self.calls += 1
        return ArchivedSource(source=self.source, payload_path=self.path)


class Parser:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = 0

    def parse(self, src, text):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        # Fake call parser supplies the same segment provenance as a real parser.
        plain = src.model_copy(update={"channel": "share_text"})
        result = text_to_thread(plain, text, timezone=ZoneInfo("Asia/Seoul"))
        return result.model_copy(update={"channel": src.channel})


class Reviser:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = 0
        self.hints = None

    def revise(self, thread, hints):
        self.calls += 1
        self.hints = hints
        if self.errors:
            raise self.errors.pop(0)
        return serialize_revision(
            Revision(
                segments=[
                    RevisionSegment(
                        segment_id=s.segment_id,
                        start=None,
                        end=None,
                        speaker=s.speaker,
                        text=thread.text[s.start_char : s.end_char] + "!",
                    )
                    for s in thread.segments
                ]
            )
        )


def pipeline(tmp_path, src=None, **kwargs):
    src = src or source()
    store = Store(tmp_path, src)
    return (
        Spinner(
            store, tmp_path, configuration_version="parser1:model1:prompt1", **kwargs
        ),
        store,
        src,
    )


def records(spinner):
    with sqlite3.connect(spinner.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY id")]


def test_share_text_exact_bytes_and_cached_result(tmp_path):
    reviser = Reviser([AssertionError("must not revise")])
    spin, store, src = pipeline(tmp_path, reviser=reviser)
    result = spin.spin(src)
    assert result.text == TEXT
    assert len(result.segments) == 1
    assert (result.segments[0].start_char, result.segments[0].end_char) == (
        0,
        len(TEXT),
    )
    assert result.source_id == src.source_id and result.occurred_at == src.occurred_at
    assert spin.spin(src) == result
    assert store.calls == 1 and reviser.calls == 0
    assert spin.path.stat().st_mode & 0o777 == 0o600
    assert Path(str(spin.path) + ".lock").stat().st_mode & 0o777 == 0o600


def test_kakao_uses_existing_parser_without_revision(tmp_path):
    text = "2026년 11월 7일 토요일\n[홍길동] [오전 9:00] 합성\n"
    src = source("kakao", text)
    store = Store(tmp_path, src, text)
    spin = Spinner(
        store, tmp_path, configuration_version="v1", reviser=Reviser([AssertionError()])
    )
    assert spin.spin(src) == TextSpinner(store, timezone=ZoneInfo("Asia/Seoul")).spin(
        src
    )
    assert any(s.speaker == "홍길동" for s in spin.spin(src).segments)


@pytest.mark.parametrize(
    "src,error",
    [
        (source("call"), "call_reviser_not_configured"),
        (source(kind="fleece"), "unsupported_kind"),
        (source("voice_memo"), "unsupported_text_channel"),
    ],
)
def test_unsupported_paths_fail_explicitly(tmp_path, src, error):
    spin, store, _ = pipeline(tmp_path, src)
    with pytest.raises(SpinnerError, match=error):
        spin.spin(src)
    assert store.calls == 0
    assert all(r["status"] == "failed" for r in records(spin))


def test_call_revision_hints_retry_and_checkpoint(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    src = source("call", hints={"counterpart": "합성 비공개 상대"})
    parser, reviser = Parser(), Reviser([SpinnerTransientError("private details")])
    sleeps = []
    spin, store, _ = pipeline(
        tmp_path, src, parsers={"call": parser}, reviser=reviser, sleep=sleeps.append
    )
    result = spin.spin(src)
    assert result.text == TEXT + "!"
    assert reviser.hints.counterpart == src.hints.counterpart
    assert parser.calls == store.calls == 1 and reviser.calls == 2
    assert sleeps == [0.25]
    assert [r["stage"] for r in records(spin)] == [
        "spin",
        "total",
        "parse",
        "revise",
        "total",
        "revise",
    ]
    for private in ("private details", TEXT, src.hints.counterpart):
        assert private not in str(records(spin))
        assert private not in caplog.text


def test_llm_failure_no_nested_retry_and_restart_reuses_parsing(tmp_path):
    parser, reviser = Parser(), Reviser([RateLimited()])
    spin, store, src = pipeline(
        tmp_path, source("call"), parsers={"call": parser}, reviser=reviser
    )
    with pytest.raises(RateLimited):
        spin.spin(src)
    assert reviser.calls == 1
    with sqlite3.connect(spin.path) as db:
        parsed, completed = db.execute("SELECT parsed,thread FROM results").fetchone()
    assert parsed and completed is None
    again = Spinner(
        store,
        tmp_path,
        configuration_version="parser1:model1:prompt1",
        parsers={"call": Parser([AssertionError()])},
        reviser=reviser,
    )
    assert again.spin(src).text == TEXT + "!"
    assert store.calls == 1


def test_parser_retry_exhaustion_backoff_cap_and_times(tmp_path):
    parser = Parser([SpinnerTransientError() for _ in range(5)])
    sleeps = []
    ticks = iter(range(100))
    spin, _, src = pipeline(
        tmp_path,
        parsers={"share_text": parser},
        settings=SpinnerSettings(5, 0.25, 0.5),
        sleep=sleeps.append,
        clock=lambda: float(next(ticks)),
    )
    with pytest.raises(SpinnerTransientError):
        spin.spin(src)
    assert parser.calls == 5 and sleeps == [0.25, 0.5, 0.5, 0.5]
    rows = records(spin)
    assert all(
        r["elapsed_seconds"] == ({"parse": 1, "total": 3, "spin": 21}[r["stage"]])
        for r in rows
    )
    assert all(r["status"] == "failed" for r in rows)


@pytest.mark.parametrize("error", [ValueError("secret"), RateLimited()])
def test_permanent_parser_errors_not_retried(tmp_path, error):
    parser = Parser([error])
    spin, _, src = pipeline(tmp_path, parsers={"share_text": parser})
    with pytest.raises(type(error)):
        spin.spin(src)
    assert parser.calls == 1


def test_reprocess_preserves_history_and_version_change_preserves_results(tmp_path):
    spin, store, src = pipeline(tmp_path)
    spin.spin(src)
    spin.spin(src, reprocess=True)
    assert store.calls == 2 and len(records(spin)) == 6
    other = Spinner(store, tmp_path, configuration_version="v2")
    other.spin(src)
    with sqlite3.connect(spin.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM results WHERE thread IS NOT NULL"
            ).fetchone()[0]
            == 2
        )
    assert len(records(spin)) == 9


def test_reprocess_failure_removes_old_success(tmp_path):
    spin, store, src = pipeline(tmp_path)
    spin.spin(src)
    store.path.write_text("corrupted")
    with pytest.raises(SpinnerError, match="payload_mismatch"):
        spin.spin(src, reprocess=True)
    with sqlite3.connect(spin.path) as db:
        assert db.execute("SELECT parsed,thread FROM results").fetchone() == (
            None,
            None,
        )


def test_interrupted_attempt_is_not_success(tmp_path):
    spin, store, src = pipeline(tmp_path)
    with sqlite3.connect(spin.path) as db:
        db.execute(
            "INSERT INTO attempts(source_id,version,attempt,stage,status) VALUES(?,?,1,'parse','running')",
            (src.source_id, "parser1:model1:prompt1"),
        )
    recovered = Spinner(store, tmp_path, configuration_version="parser1:model1:prompt1")
    assert records(recovered)[0]["status"] == "interrupted"
    assert records(recovered)[0]["elapsed_seconds"] is None
    assert recovered.spin(src).text == TEXT


def test_parallel_spins_serialize_and_publish_once(tmp_path):
    entered, release = Event(), Event()

    class Blocking(Parser):
        def parse(self, src, text):
            entered.set()
            assert release.wait(5)
            return super().parse(src, text)

    parser = Blocking()
    spin, store, src = pipeline(tmp_path, parsers={"share_text": parser})
    other = Spinner(
        store,
        tmp_path,
        configuration_version="parser1:model1:prompt1",
        parsers={"share_text": parser},
    )
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(spin.spin, src)
        assert entered.wait(5)
        second = pool.submit(other.spin, src)
        with sqlite3.connect(spin.path) as db:
            assert db.execute("SELECT thread FROM results").fetchone()[0] is None
        release.set()
        assert first.result() == second.result()
    assert parser.calls == store.calls == 1


def test_provenance_contract_rejected(tmp_path):
    class Wrong(Parser):
        def parse(self, src, text):
            return super().parse(src, text).model_copy(update={"channel": "kakao"})

    spin, _, src = pipeline(tmp_path, parsers={"share_text": Wrong()})
    with pytest.raises(SpinnerError, match="provenance"):
        spin.spin(src)


@pytest.mark.parametrize(
    "updates",
    [
        dict(max_attempts=0),
        dict(max_attempts=True),
        dict(backoff_seconds=-1),
        dict(backoff_seconds=float("inf")),
        dict(max_backoff_seconds=0.1),
        dict(backoff_seconds="secret"),
    ],
)
def test_retry_settings_validate(updates):
    with pytest.raises(ConfigError):
        replace(SpinnerSettings(), **updates)


def test_process_death_during_revision_recovers_committed_checkpoint(tmp_path):
    import subprocess
    import sys

    script = """
import os
import sys
from pathlib import Path
from server.tests.spinner.test_spinner import Parser, Store, source
from weaver.spinner import Spinner
src = source("call")
class KilledReviser:
    def revise(self, thread, hints):
        os._exit(23)
root = Path(sys.argv[1])
Spinner(Store(root, src), root, configuration_version="v1",
        parsers={"call": Parser()}, reviser=KilledReviser()).spin(src)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=False)
    assert result.returncode == 23
    with sqlite3.connect(tmp_path / "spinner.sqlite3") as db:
        parsed, thread = db.execute("SELECT parsed,thread FROM results").fetchone()
        assert parsed and thread is None
    src = source("call")
    parser = Parser([AssertionError("must reuse persisted parse")])
    spin = Spinner(
        Store(tmp_path, src),
        tmp_path,
        configuration_version="v1",
        parsers={"call": parser},
        reviser=Reviser(),
    )
    assert sum(r["status"] == "interrupted" for r in records(spin)) == 3
    assert spin.spin(src).text == TEXT + "!"
    assert parser.calls == 0


def test_call_with_llm_reviser_adapter(tmp_path):
    from pydantic import BaseModel

    from weaver.spinner.reviser import LlmReviser

    class Client:
        def generate_json[T: BaseModel](
            self, task: str, system: str, user: str, schema: type[T]
        ) -> T:
            payload = json.loads(user)
            assert payload["hints"]["counterpart"] == src.hints.counterpart
            return schema.model_validate(
                {
                    "segments": [
                        {"segment_id": s["segment_id"], "text": s["text"] + "!"}
                        for s in payload["segments"]
                    ]
                }
            )

        def generate_text(self, task: str, system: str, user: str) -> str:
            raise AssertionError()

    src = source("call")
    spin, _, _ = pipeline(
        tmp_path, src, parsers={"call": Parser()}, reviser=LlmReviser(Client())
    )
    assert spin.spin(src).text == TEXT + "!"


def test_timezone_change_creates_independent_result(tmp_path):
    text = "--------------- 2026년 11월 7일 토요일 ---------------\n[합성] [오전 9:00] 테스트\n"
    src = source("kakao", text)
    store = Store(tmp_path, src, text)
    seoul = Spinner(
        store, tmp_path, configuration_version="v1", timezone=ZoneInfo("Asia/Seoul")
    )
    utc = Spinner(store, tmp_path, configuration_version="v1", timezone=ZoneInfo("UTC"))
    assert (
        seoul.spin(src).segments[-1].occurred_at
        != utc.spin(src).segments[-1].occurred_at
    )
    with sqlite3.connect(seoul.path) as db:
        assert (
            db.execute(
                "SELECT count(DISTINCT version) FROM results WHERE thread IS NOT NULL"
            ).fetchone()[0]
            == 2
        )
    assert store.calls == 2


@pytest.mark.parametrize(
    "component",
    ["parser", "reviser", "builtin_parser", "prompt", "model", "prompt_text"],
)
def test_internal_version_changes_invalidate_cache(tmp_path, monkeypatch, component):
    import weaver.spinner.pipeline as module
    import weaver.spinner.reviser as revision
    from weaver.llm import AnthropicClient
    from weaver.spinner.reviser import LlmReviser

    class Client(AnthropicClient):
        def __init__(self):
            self.calls = 0

        def generate_json(self, task, system, user, schema):
            self.calls += 1
            payload = json.loads(user)
            return schema.model_validate(
                {
                    "segments": [
                        {"segment_id": s["segment_id"], "text": s["text"] + "!"}
                        for s in payload["segments"]
                    ]
                }
            )

    parser, reviser = Parser(), Reviser()
    client = Client()
    adapter = LlmReviser(client) if component in {"model", "prompt_text"} else reviser
    spin, store, src = pipeline(
        tmp_path, source("call"), parsers={"call": parser}, reviser=adapter
    )
    spin.spin(src)
    if component == "parser":
        parser.version = "v2"
    elif component == "reviser":
        reviser.version = "v2"
    elif component == "builtin_parser":
        monkeypatch.setattr(module, "PARSER_VERSION", "v2")
    elif component == "prompt":
        monkeypatch.setattr(module, "PROMPT_VERSION", "v2")
    elif component == "model":
        monkeypatch.setattr(revision, "MODEL", "new-model")
    else:
        monkeypatch.setattr(revision, "SYSTEM_PROMPT", "new-prompt")
    other = Spinner(
        store,
        tmp_path,
        configuration_version="parser1:model1:prompt1",
        parsers={"call": parser},
        reviser=adapter,
    )
    other.spin(src)
    assert parser.calls == 2
    assert (client.calls if isinstance(adapter, LlmReviser) else reviser.calls) == 2
    with sqlite3.connect(spin.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM results WHERE thread IS NOT NULL"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("stage", ["parse", "revise"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_control_flow_exceptions_are_interrupted(tmp_path, stage, error_type):
    parser = Parser([error_type()] if stage == "parse" else [])
    reviser = Reviser([error_type()] if stage == "revise" else [])
    spin, _, src = pipeline(
        tmp_path, source("call"), parsers={"call": parser}, reviser=reviser
    )
    with pytest.raises(error_type):
        spin.spin(src)
    interrupted = [r for r in records(spin) if r["stage"] in {"spin", "total", stage}]
    assert all(r["status"] == r["error_class"] == "interrupted" for r in interrupted)


def test_existing_instance_recovers_dead_owners_at_spin(tmp_path):
    spin, _, src = pipeline(tmp_path)
    with sqlite3.connect(spin.path) as db:
        db.execute(
            "INSERT INTO attempts(source_id,version,attempt,stage,status) VALUES(?,?,1,'parse','running')",
            (src.source_id, "old-owner"),
        )
    spin.spin(src)
    assert records(spin)[0]["status"] == "interrupted"


@pytest.mark.parametrize("stage", ["parse", "revise", "revalidate"])
def test_adapter_validation_errors_are_private(tmp_path, caplog, stage):
    import traceback

    from weaver.schema.models import Thread

    counterpart = "합성 비공개 상대"
    secret = TEXT + counterpart

    class InvalidParser(Parser):
        def parse(self, src, text):
            if stage == "parse":
                return Thread.model_validate({"text": secret})
            result = super().parse(src, text)
            if stage == "revalidate":
                return result.model_copy(update={"schema_version": secret})
            return result

    class InvalidReviser(Reviser):
        def revise(self, thread, hints):
            return Thread.model_validate({"text": secret})

    spin, _, src = pipeline(
        tmp_path,
        source("call", hints={"counterpart": counterpart}),
        parsers={"call": InvalidParser()},
        reviser=InvalidReviser(),
    )
    with (
        caplog.at_level(logging.INFO),
        pytest.raises(SpinnerError, match="^invalid_adapter_thread$") as caught,
    ):
        spin.spin(src)
    rendered = "".join(traceback.format_exception(caught.value))
    for private in (TEXT, src.hints.counterpart):
        assert private not in rendered
        assert private not in caplog.text
        assert private not in str(records(spin))
    assert caught.value.__suppress_context__


def test_missing_reviser_fails_before_parser_or_payload(tmp_path):
    parser = Parser()
    spin, store, src = pipeline(tmp_path, source("call"), parsers={"call": parser})
    with pytest.raises(SpinnerError, match="call_reviser_not_configured"):
        spin.spin(src)
    assert parser.calls == store.calls == 0
    assert [r["stage"] for r in records(spin)] == ["spin"]


def test_missing_state_root_is_created_private_and_wal(tmp_path):
    src = source()
    state = tmp_path / "nested" / "state"
    spin = Spinner(Store(tmp_path, src), state, configuration_version="v1")
    assert state.stat().st_mode & 0o777 == 0o700
    assert spin.spin(src).text == TEXT
    with sqlite3.connect(spin.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_payload_symlink_is_rejected(tmp_path):
    spin, store, src = pipeline(tmp_path)
    original = tmp_path / "original"
    store.path.rename(original)
    store.path.symlink_to(original)
    with pytest.raises(OSError):
        spin.spin(src)
    with sqlite3.connect(spin.path) as db:
        assert db.execute("SELECT thread FROM results").fetchone()[0] is None


@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_revision_success_and_result_commit_atomically_across_crash(tmp_path, phase):
    import subprocess
    import sys

    # A pre-existing instance must also recover attempts left by the child.
    src = source("call")
    reviser = Reviser()
    spin = Spinner(
        Store(tmp_path, src),
        tmp_path,
        configuration_version="v1",
        parsers={"call": Parser()},
        reviser=reviser,
    )
    script = """
import os
import sys
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from server.tests.spinner.test_spinner import Parser, Reviser, Store, source
from weaver.spinner import Spinner
class CrashSpinner(Spinner):
    @contextmanager
    def _connect(self):
        with super()._connect() as db:
            yield db
            if sys.argv[2] == "before_commit" and db.execute(
                "SELECT 1 FROM attempts WHERE stage='revise' AND status='succeeded'"
            ).fetchone():
                os._exit(23)
    def _finish(self, record, started, error, **kwargs):
        super()._finish(record, started, error, **kwargs)
        if sys.argv[2] == "after_commit" and error is None:
            with sqlite3.connect(self.path) as db:
                stage = db.execute("SELECT stage FROM attempts WHERE id=?", (record,)).fetchone()[0]
            if stage == "revise":
                os._exit(23)
root = Path(sys.argv[1])
src = source("call")
CrashSpinner(Store(root, src), root, configuration_version="v1",
             parsers={"call": Parser()}, reviser=Reviser()).spin(src)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), phase], check=False
    )
    assert child.returncode == 23
    with sqlite3.connect(spin.path) as db:
        parsed, thread = db.execute("SELECT parsed,thread FROM results").fetchone()
        succeeded = db.execute(
            "SELECT count(*) FROM attempts WHERE stage='revise' AND status='succeeded'"
        ).fetchone()[0]
    assert parsed
    assert bool(thread) == bool(succeeded) == (phase == "after_commit")
    assert spin.spin(src).text == TEXT + "!"
    assert reviser.calls == (0 if phase == "after_commit" else 1)
    assert not any(r["status"] == "running" for r in records(spin))


def test_builtin_galaxy_call_parser_in_pipeline(tmp_path):
    text = "나  (00:01)\n가짜 대화\n\n가상상대  (00:03)\n가짜 응답\n"
    src = source("call", text=text)
    store = Store(tmp_path, src, text)
    reviser = Reviser()
    spin = Spinner(store, tmp_path, configuration_version="builtin", reviser=reviser)
    result = spin.spin(src)
    assert [s.speaker for s in result.segments if s.speaker] == ["나", "가상상대"]
    assert reviser.calls == 1 and reviser.hints.counterpart == src.hints.counterpart
    with sqlite3.connect(spin.path) as db:
        parsed = json.loads(db.execute("SELECT parsed FROM results").fetchone()[0])
    messages = [s for s in parsed["segments"] if s["speaker"]]
    assert [s["start_ms"] for s in messages] == [1000, 3000]
    assert [s["end_ms"] for s in messages] == [3000, None]
    assert parsed["text"] == text
