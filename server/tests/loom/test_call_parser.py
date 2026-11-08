"""Invented calls only; no real calls, LLMs, speech models or vault access."""

from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

import pytest
from pydantic import ValidationError

from weaver.loom.call import CallParseError, GalaxyCallParser, parse_call
from weaver.schema import Thread
from weaver.spinner import TextParser

from .test_kakao_parser import source


def call_source(occurred_at=None):
    return source("call").model_copy(update={"occurred_at": occurred_at})


def assert_coverage(result, text):
    thread = result.thread
    assert thread.text == text
    assert thread.segments[0].start_char == 0
    assert thread.segments[-1].end_char == len(text)
    assert all(
        a.end_char == b.start_char
        for a, b in zip(thread.segments, thread.segments[1:], strict=False)
    )
    assert "".join(text[s.start_char : s.end_char] for s in thread.segments) == text
    assert Thread.model_validate_json(thread.model_dump_json()) == thread
    for diagnostic in result.diagnostics:
        assert 0 <= diagnostic.start_char < diagnostic.end_char <= len(text)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
@pytest.mark.parametrize("known_time", [False, True])
def test_synthetic_fixture(newline, bom, known_time):
    text = bom + (Path(__file__).parent / "fixtures/call.txt").read_text().replace(
        "\n", newline
    )
    occurred_at = (
        datetime.fromisoformat("2026-11-08T12:00:00+09:00") if known_time else None
    )
    src = call_source(occurred_at)
    result = parse_call(src, text)
    assert_coverage(result, text)
    assert result.thread.occurred_at == occurred_at
    messages = [s for s in result.thread.segments if s.speaker]
    assert [s.speaker for s in messages] == ["나", "홍길동", "나 & 홍길동"]
    assert [s.start_ms for s in messages] == [1000, 3000, 3607000]
    assert [s.end_ms for s in messages] == [3000, 3607000, None]
    assert [s.occurred_at for s in messages] == [
        occurred_at + timedelta(milliseconds=s.start_ms) if occurred_at else None
        for s in messages
    ]
    assert [d.code for d in result.diagnostics] == ["overlap_speakers"]
    assert all(
        s.occurred_at is None and s.start_ms is None
        for s in result.thread.segments
        if s.speaker is None
    )


def test_empty_header_only_equal_and_regressing_times():
    text = "나  (00:03)\n\n가상  (00:03)\n나  (00:01)"
    result = parse_call(call_source(), text)
    assert_coverage(result, text)
    messages = [s for s in result.thread.segments if s.speaker]
    assert len(messages) == 3 and all(s.end_ms is None for s in messages)
    assert sum(d.code == "empty_utterance" for d in result.diagnostics) == 3
    assert sum(d.code == "non_increasing_time" for d in result.diagnostics) == 2


@pytest.mark.parametrize(
    "clock", ["00:60", "60:00", "1:60:00", "１:00:00", "0:1", "9" * 5000 + ":00:00"]
)
def test_invalid_clocks_are_preserved(clock):
    text = f"AI로 생성된 콘텐츠입니다\n\n가짜 제목\n\n나  ({clock})\n가짜 본문\n"
    result = parse_call(call_source(), text)
    assert_coverage(result, text)
    assert not any(s.speaker for s in result.thread.segments)
    assert "invalid_header" in [d.code for d in result.diagnostics]


def test_unknown_surplus_lines_and_unicode_separator():
    text = "AI로 생성된 콘텐츠입니다\n\n가짜 제목\n\n나  (00:01)\n가짜\u2028본문 \n모르는 추가 줄\n\n깨진 화자  (99:99)\n"
    result = parse_call(call_source(), text)
    assert_coverage(result, text)
    assert {d.code for d in result.diagnostics} == {
        "unrecognized_line",
        "invalid_header",
    }


@pytest.mark.parametrize(
    "text,code",
    [
        (" \n", "empty_input"),
        ("모르는 형식", "unsupported_format"),
        ("\x00가짜", "unsupported_encoding"),
    ],
)
def test_safe_errors(text, code):
    with pytest.raises(CallParseError) as error:
        parse_call(call_source(), text)
    assert str(error.value) == code


def test_timestamp_overflow_is_diagnostic():
    result = parse_call(
        call_source(datetime.max.replace(tzinfo=datetime.now().astimezone().tzinfo)),
        "나  (00:01)\n가짜\n",
    )
    assert result.thread.segments[0].occurred_at is None
    assert "occurred_at_overflow" in [d.code for d in result.diagnostics]


def test_protocol_adapter_and_logs_do_not_leak(caplog):
    parser: TextParser = GalaxyCallParser()
    text = "비밀가상 & 가상상대  (00:01)\n비밀가상본문\n"
    thread = parser.parse(call_source(), text)
    assert thread.text == text
    assert "call:overlap_speakers count=1" in caplog.text
    assert "비밀가상" not in caplog.text and "가상상대" not in caplog.text


@pytest.mark.parametrize(
    "line",
    [
        "(" * 1_000_000,
        "가상  (" * 150_000 + "99:99)",
        "나  (" + "9" * 1_000_000 + ":00:00)",
    ],
)
def test_long_adversarial_lines_remain_linear(line):
    text = "AI로 생성된 콘텐츠입니다\n\n가짜 제목\n\n" + line + "\n"
    started = monotonic()
    result = parse_call(call_source(), text)
    assert monotonic() - started < 5
    assert_coverage(result, text)


def test_nonnull_end_still_requires_later_time():
    payload = parse_call(call_source(), "나  (00:01)\n가짜\n").thread.model_dump()
    payload["segments"][0]["end_ms"] = 1000
    with pytest.raises(ValidationError):
        Thread.model_validate(payload)


def test_missing_preamble_and_block_separator_are_diagnosed():
    text = "나  (00:01)\n가짜 본문\n상대  (00:02)\n가짜 응답\n"
    result = parse_call(call_source(), text)
    assert_coverage(result, text)
    assert {d.code for d in result.diagnostics} == {
        "missing_preamble",
        "missing_separator",
    }
