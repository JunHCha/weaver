"""Only invented conversations; no user vault, LLM or speech model access."""

import hashlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from weaver.loom.kakao import KakaoParseError, parse_kakao
from weaver.schema import Source
from weaver.spinner.text import TextSpinner, text_to_thread

FIXTURES = Path(__file__).parent / "fixtures"
ZONE = ZoneInfo("Asia/Seoul")


def source(channel="kakao", body=b"fake"):
    return Source(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        capture_id="cap_01ARZ3NDEKTSV4RRFFQ69G5FA0",
        kind="text",
        channel=channel,
        captured_at=datetime.fromisoformat("2026-11-07T16:00:00+09:00"),
        occurred_at=None,
        sha256=hashlib.sha256(body).hexdigest(),
        hints={"counterpart": None},
        payload_ref="payloads/fake.txt",
    )


def read_fixture(name):
    return (FIXTURES / name).read_bytes().decode("utf-8")


def assert_coverage(result, text):
    assert result.thread.text == text
    segments = result.thread.segments
    assert segments[0].start_char == 0
    assert segments[-1].end_char == len(text)
    assert all(
        a.end_char == b.start_char for a, b in zip(segments, segments[1:], strict=False)
    )
    assert all(s.start_ms is s.end_ms is None for s in segments)
    assert "".join(text[s.start_char : s.end_char] for s in segments) == text


def test_pc_dates_multiline_system_and_unicode():
    text = read_fixture("pc.txt")
    original = source()
    result = parse_kakao(original, text, timezone=ZONE)
    assert_coverage(result, text)
    assert result.thread.source_id == original.source_id
    assert result.thread.occurred_at is None
    messages = [s for s in result.thread.segments if s.speaker]
    assert [s.speaker for s in messages] == ["홍길동", "김가상", "홍길동", "김가상"]
    assert [s.occurred_at.isoformat() for s in messages] == [
        "2026-11-06T00:00:00+09:00",
        "2026-11-06T12:00:00+09:00",
        "2026-11-06T23:59:00+09:00",
        "2026-11-07T00:00:00+09:00",
    ]
    body = text[messages[0].start_char : messages[0].end_char]
    assert "😀" in body and "둘째 줄: A, B" in body
    assert "[인용] [오전 xx:yy]" in body
    assert any(
        "들어왔습니다" in text[s.start_char : s.end_char] and s.speaker is None
        for s in result.thread.segments
    )
    assert any(d.code == "ambiguous_boundary" for d in result.diagnostics)


def test_mobile_bom_crlf_date_variants_and_timestamp_system():
    text = read_fixture("mobile.txt")
    assert text.startswith("\ufeff") and "\r\n" in text
    result = parse_kakao(source(), text, timezone=ZONE)
    assert_coverage(result, text)
    messages = [s for s in result.thread.segments if s.speaker]
    assert [s.speaker for s in messages] == ["홍길동", "김가상"]
    assert [s.occurred_at.hour for s in messages] == [0, 13]
    assert "본문: 쉼표, 유지" in text[messages[0].start_char : messages[0].end_char]
    assert all(s.occurred_at is None for s in result.thread.segments if not s.speaker)


def test_exact_message_lookalike_is_split_with_diagnostic():
    text = "2026년 11월 7일 토요일\n[홍길동] [오전 9:00] 인용:\n이어지는 인용 본문\n[김가상] [오전 9:01] 실제와 구별 불가\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    assert len([s for s in result.thread.segments if s.speaker]) == 2
    boundary = next(d for d in result.diagnostics if d.code == "ambiguous_boundary")
    assert boundary.line == 4
    assert text[boundary.start_char :].startswith("[김가상]")


@pytest.mark.parametrize("bad", ["2026년 13월 7일 토요일", "2026년 2월 30일 월요일"])
def test_invalid_date_resets_context_without_losing_text(bad):
    text = f"2026년 11월 7일 토요일\n[홍길동] [오전 9:00] 정상\n{bad}\n[김가상] [오후 1:00] 날짜 모름\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    assert_coverage(result, text)
    assert [s for s in result.thread.segments if s.speaker][-1].occurred_at is None
    assert "invalid_date" in [d.code for d in result.diagnostics]
    assert "missing_date" in [d.code for d in result.diagnostics]


def test_broken_headers_and_partial_lookalikes_are_preserved():
    text = "알 수 없는 머리말\n2026년 11월 7일 토요일\n[홍길동] [오전 9:00] 시작\n[김가상] [오후 25:00] 깨진 시각\n[김가상] 비슷한 본문\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    assert_coverage(result, text)
    assert "unrecognized_line" in [d.code for d in result.diagnostics]
    assert "invalid_time" in [d.code for d in result.diagnostics]
    assert len([s for s in result.thread.segments if s.speaker]) == 1


@pytest.mark.parametrize(
    "text,code",
    [
        ("", "empty_input"),
        (" \r\n\ufeff", "empty_input"),
        ("Chat export\nNovember 7, 2026, 1:00 PM, Fake : hello", "unsupported_format"),
    ],
)
def test_empty_or_unsupported_has_explicit_safe_error(text, code):
    with pytest.raises(KakaoParseError) as error:
        parse_kakao(source(), text, timezone=ZONE)
    assert error.value.code == code
    assert str(error.value) == code


def test_timezone_is_injected_and_capture_time_is_never_message_time():
    text = "2026년 11월 7일 토요일\n[홍길동] [오전 1:00] 합성\n"
    result = parse_kakao(source(), text, timezone=ZoneInfo("America/New_York"))
    assert (
        result.thread.segments[-1].occurred_at.isoformat()
        == "2026-11-07T01:00:00-05:00"
    )
    assert result.thread.occurred_at is None


def test_share_text_bypasses_kakao_and_preserves_every_character():
    text = "\ufeff[가짜] [오전 9:00] 그대로\r\n😀"
    thread = text_to_thread(source("share_text"), text, timezone=ZONE)
    assert thread.text == text
    assert len(thread.segments) == 1
    assert thread.segments[0].speaker is thread.segments[0].occurred_at is None


@pytest.mark.parametrize("encoding", ["cp949", "utf-16", "utf-16-le", "utf-16-be"])
def test_spinner_rejects_unsupported_encoding(tmp_path, encoding):
    from weaver.schema.internal import ArchivedSource

    path = tmp_path / "fake.txt"
    body = "합성 카카오톡 대화".encode(encoding)
    path.write_bytes(body)
    original = source(body=body)

    class Store:
        def locate(self, source_id):
            return ArchivedSource(source=original, payload_path=path)

    with pytest.raises(KakaoParseError, match="unsupported_encoding"):
        TextSpinner(Store(), timezone=ZONE).spin(original)


def test_archive_store_to_spinner(tmp_path):
    from weaver.archive import ArchiveStore
    from weaver.schema import Manifest

    archive, receipts = tmp_path / "archive", tmp_path / "receipts"
    archive.mkdir()
    receipts.mkdir()
    payload = tmp_path / "fake.txt"
    body = (FIXTURES / "mobile.txt").read_bytes()
    payload.write_bytes(body)
    original = source(body=body)
    manifest = Manifest.model_validate(
        original.model_dump(exclude={"source_id", "payload_ref"})
        | {"payload": original.capture_id + ".txt", "size": len(body)}
    )
    store = ArchiveStore(archive, receipts)
    stored = store.put(manifest, payload)
    spinner = TextSpinner(store, timezone=ZONE)
    thread = spinner.spin(stored)
    assert thread.source_id == stored.source_id
    assert thread.text == body.decode("utf-8")
    assert [s.speaker for s in thread.segments if s.speaker] == ["홍길동", "김가상"]


@pytest.mark.parametrize(
    "stamp", ["2026년 11월 1일 오전 1:30", "2026년 3월 8일 오전 2:30"]
)
def test_dst_ambiguous_or_nonexistent_local_time_stays_unknown(stamp):
    text = stamp + ", 홍길동 : 합성 메시지\n"
    result = parse_kakao(source(), text, timezone=ZoneInfo("America/New_York"))
    assert result.thread.segments[0].occurred_at is None
    assert [d.code for d in result.diagnostics] == ["ambiguous_local_time"]


def test_header_only_and_malformed_header_preserve_or_reject_explicitly():
    text = "프로젝트 A 님과 카카오톡 대화\n알 수 없는 머리말\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    assert_coverage(result, text)
    assert {d.code for d in result.diagnostics} == {"no_messages", "unrecognized_line"}
    with pytest.raises(KakaoParseError, match="unsupported_format"):
        parse_kakao(source(), "깨진 카카오 헤더\n알 수 없는 형식", timezone=ZONE)


def test_thread_occurrence_is_source_metadata_not_first_message():
    original = source().model_copy(
        update={"occurred_at": datetime.fromisoformat("2026-11-01T08:00:00+09:00")}
    )
    result = parse_kakao(original, read_fixture("pc.txt"), timezone=ZONE)
    assert result.thread.occurred_at == original.occurred_at


def test_text_spinner_from_config_injects_common_timezone(tmp_path):
    from weaver.config import load_config
    from weaver.schema.internal import ArchivedSource

    payload = tmp_path / "fake.txt"
    payload.write_text("2026년 11월 7일 오전 1:00, 홍길동 : 가짜\n")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            f'{key}_root = "{key}"'
            for key in ("sync", "archive", "vault", "state", "log")
        )
        + '\n[time]\ntimezone = "UTC"\n'
    )
    config = load_config(config_path, environ={}, init_directories=True)

    original = source(body=payload.read_bytes())

    class Store:
        def locate(self, source_id):
            return ArchivedSource(source=original, payload_path=payload)

    thread = TextSpinner.from_config(Store(), config).spin(original)
    assert thread.segments[0].occurred_at.isoformat() == "2026-11-07T01:00:00+00:00"


def test_parser_diagnostics_and_spinner_logs_never_quote_conversation(caplog):
    text = "프로젝트 A 님과 카카오톡 대화\n개인내용대신가짜비밀\n2026년 11월 7일 토요일\n[홍길동] [오전 1:00] 합성\n[김가상] [오전 1:01] 합성\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    text_to_thread(source(), text, timezone=ZONE)
    assert "개인내용대신가짜비밀" not in repr(result.diagnostics)
    assert "홍길동" not in caplog.text and "개인내용대신가짜비밀" not in caplog.text


def test_unicode_line_separator_is_message_data():
    text = "2026년 11월 7일 토요일\n[홍길동] [오전 1:00] 앞\u2028뒤\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    assert len(result.thread.segments) == 2
    assert_coverage(result, text)


@pytest.mark.parametrize(
    "continuation", ["2027년 계획은 계속합니다", "2026. 11 회의록", "2026년 깨진 날짜"]
)
def test_date_like_body_keeps_message_and_pc_date(continuation):
    text = f"2026년 11월 7일 토요일\n[홍길동] [오전 9:00] 시작\n{continuation}\n[김가상] [오전 10:00] 다음\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    assert_coverage(result, text)
    messages = [s for s in result.thread.segments if s.speaker]
    assert len(result.thread.segments) == 3
    assert continuation in text[messages[0].start_char : messages[0].end_char]
    assert messages[1].occurred_at.isoformat() == "2026-11-07T10:00:00+09:00"
    assert "missing_date" not in [d.code for d in result.diagnostics]


def test_plain_message_boundaries_do_not_emit_ambiguity():
    text = "2026년 11월 7일 토요일\n" + "[홍길동] [오전 9:00] 합성\n" * 1000
    result = parse_kakao(source(), text, timezone=ZONE)
    assert len(result.thread.segments) == 1001
    assert not result.diagnostics


def test_repeated_diagnostics_log_one_summary_per_code(caplog):
    text = (
        "2026년 11월 7일 토요일\n" + "[홍길동] [오전 9:00] 합성\n이어지는 본문\n" * 100
    )
    text_to_thread(source(), text, timezone=ZONE)
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == "kakao:ambiguous_boundary count=99"


@pytest.mark.parametrize(
    "line",
    [
        "가님이 " * 32000 + "초대했습니다X",
        "가님이 " * 32000 + "김가상님을 초대했습니다.",
        "가" * 128000 + "님을 초대했습니다.",
    ],
)
def test_long_system_lookalike_line_is_processed_without_quadratic_backtracking(line):
    from time import perf_counter

    text = "2026년 11월 7일 토요일\n[홍길동] [오전 9:00] 합성\n" + line + "\n"
    start = perf_counter()
    result = parse_kakao(source(), text, timezone=ZONE)
    elapsed = perf_counter() - start
    assert elapsed < 1.0, f"128k-character line took {elapsed:.3f}s"
    assert_coverage(result, text)
    assert len(result.thread.segments) in (2, 3)


@pytest.mark.parametrize(
    "line",
    [
        "홍길동님이 들어왔습니다.",
        "홍길동님이 나갔습니다.",
        "홍길동님이 김가상님을 초대했습니다.",
        "2026년 11월 7일 오후 1:00, 홍길동님이 김가상님을 초대했습니다.",
    ],
)
def test_system_forms_still_get_null_metadata(line):
    text = "2026년 11월 7일 토요일\n" + line + "\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    assert_coverage(result, text)
    assert all(s.speaker is s.occurred_at is None for s in result.thread.segments)


@pytest.mark.parametrize(
    "last",
    [
        "[김가상] [오전 9:00] 역전",
        "2026년 11월 8일 오전 11:00, 김가상 : 헤더와 불일치",
        "2026년 11월 6일 오전 11:00, 김가상 : 역전과 불일치",
    ],
)
def test_time_regression_and_header_mismatch_warn_without_correction(last):
    text = "2026년 11월 7일 토요일\n[홍길동] [오전 10:00] 이전\n" + last + "\n"
    result = parse_kakao(source(), text, timezone=ZONE)
    warnings = [d for d in result.diagnostics if d.code == "time_regression"]
    assert len(warnings) == 1 and warnings[0].line == 3
    last_segment = result.thread.segments[-1]
    expected = (
        "2026-11-07T09:00:00+09:00"
        if last.startswith("[")
        else (
            "2026-11-08T11:00:00+09:00"
            if "8일" in last
            else "2026-11-06T11:00:00+09:00"
        )
    )
    assert last_segment.occurred_at.isoformat() == expected


@pytest.mark.parametrize("channel", ["kakao", "share_text"])
def test_spinner_rejects_payload_hash_mismatch_without_modification(tmp_path, channel):
    from weaver.schema.internal import ArchivedSource

    body = b"changed synthetic text"
    path = tmp_path / "fake.txt"
    path.write_bytes(body)
    original = source(channel, body=b"original synthetic text")

    class Store:
        def locate(self, source_id):
            return ArchivedSource(source=original, payload_path=path)

    with pytest.raises(KakaoParseError, match="^payload_mismatch$"):
        TextSpinner(Store(), timezone=ZONE).spin(original)
    assert path.read_bytes() == body
