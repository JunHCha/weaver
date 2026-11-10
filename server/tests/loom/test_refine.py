"""Synthetic Korean conversations only; no LLM, speech engine or vault IO."""

from datetime import datetime

import pytest

from weaver.config import RefinerConfig
from weaver.loom.refine import RuleRefiner
from weaver.schema import Script
from weaver.schema.models import Segment

STAMP = datetime.fromisoformat("2026-11-07T23:30:00+09:00")


def thread(text: str, *, parts: list[str] | None = None) -> Script:
    segments = []
    offset = 0
    for i, part in enumerate(parts or [], start=1):
        start = text.index(part, offset)
        segments.append(
            Segment(
                segment_id=f"s{i}",
                start_char=start,
                end_char=start + len(part),
                start_ms=None,
                end_ms=None,
                speaker=f"화자{i}",
                occurred_at=STAMP,
            )
        )
        offset = start + len(part)
    return Script(
        schema_version=1,
        source_id="src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        channel="kakao",
        occurred_at=STAMP,
        text=text,
        segments=segments,
    )


@pytest.mark.parametrize(
    "signal",
    [
        "내일",
        "다음 주",
        "이번 주 금요일",
        "11월 8일",
        "오후 3시",
        "3시 반",
        "오전 12시",
        "홍길동 님",
        "팀장님",
        "오늘은",
        "어제",
        "모레",
        "그제",
        "지난달",
        "이번 해",
        "월요일에",
        "2026년 11월 8일",
        "2026년",
        "2026-11-08",
        "2026. 11. 8.",
        "15:30",
        "3일 동안",
        "두 시간",
        "한 달",
        "대표님께",
        "김하나씨",
        "선생님",
        "박사님",
    ],
)
def test_positive_patterns(signal: str) -> None:
    source = thread(f"{signal} 이야기합시다.")
    selected = RuleRefiner().select(source)
    assert len(selected) == 1
    assert selected[0].triggers
    selected[0].validate_thread(source)


@pytest.mark.parametrize(
    "text",
    [
        "12345",
        "좋은 이야기입니다.",
        "내일림 다음 주스",
        "일요일기",
        "오후반",
        "3시스템",
        "오후 3시스템",
        "11월드",
        "팀장난",
        "홍길동 님비",
        "김하나씨앗",
        "99월 99일",
        "오후 33시",
        "25:99",
        "3시 99분",
        "123시",
        "12345분",
    ],
)
def test_negative_patterns(text: str) -> None:
    assert RuleRefiner().select(thread(text)) == []


def test_windows_merge_and_original_offsets() -> None:
    text = "😀 인사. 내일 만나요! 준비해요. 팀장님 오세요? 끝이에요. 제외."
    source = thread(text)
    candidates = RuleRefiner().select(source)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.context == text[: text.index(" 제외.")]
    assert (candidate.start_sentence, candidate.end_sentence) == (0, 5)
    assert [text[t.start_char : t.end_char] for t in candidate.triggers] == [
        "내일",
        "팀장님",
    ]
    assert [t.sentence_index for t in candidate.triggers] == [1, 3]
    assert candidate.context == text[candidate.start_char : candidate.end_char]
    assert source.text == text
    assert candidates == RuleRefiner().select(source)


def test_zero_window_and_edges() -> None:
    source = thread("내일 만나요. 평범한 문장. 또 평범해요. 팀장님!")
    candidates = RuleRefiner(RefinerConfig(before=0, after=0)).select(source)
    assert [c.context for c in candidates] == ["내일 만나요.", "팀장님!"]
    assert [c.start_sentence for c in candidates] == [0, 3]


def test_message_boundaries_multiple_speakers_and_reference() -> None:
    parts = ["인사", "내일 만나요", "팀장님 오세요", "종료", "제외"]
    source = thread("\n".join(parts), parts=parts)
    other_stamp = datetime.fromisoformat("2026-11-08T01:00:00-05:00")
    source.segments[1].occurred_at = other_stamp
    selected = RuleRefiner().select(source)
    assert len(selected) == 1
    candidate = selected[0]
    assert candidate.segment_ids == ["s1", "s2", "s3", "s4"]
    assert candidate.triggers[0].segment_ids == ["s2"]
    assert candidate.triggers[0].reference_at == other_stamp
    assert candidate.triggers[0].reference_at.utcoffset() == other_stamp.utcoffset()
    assert candidate.context == "\n".join(parts[:4])
    candidate.validate_thread(source)


def test_unpunctuated_adjacent_segments_are_separate_units() -> None:
    source = thread("인사내일 만나요종료", parts=["인사", "내일 만나요", "종료"])
    selected = RuleRefiner(RefinerConfig(before=0, after=0)).select(source)
    assert selected[0].context == "내일 만나요"
    assert selected[0].segment_ids == ["s2"]


def test_no_speaker_and_missing_reference_never_uses_clock() -> None:
    source = thread("인사\n내일 이야기해요\n끝")
    source.channel = "call"
    source.occurred_at = None
    selected = RuleRefiner().select(source)
    assert selected[0].triggers[0].reference_at is None
    assert selected[0].segment_ids == []


def test_segment_without_timestamp_falls_back_to_thread() -> None:
    source = thread("내일 봐요", parts=["내일 봐요"])
    source.segments[0].occurred_at = None
    assert RuleRefiner().select(source)[0].triggers[0].reference_at == STAMP


def test_numeric_separators_do_not_split_sentences() -> None:
    source = thread("금액 3.14예요. 2026. 11. 8. 만나요. 끝.")
    result = RuleRefiner(RefinerConfig(before=0, after=0)).select(source)
    assert result[0].context == "2026. 11. 8."
    assert result[0].start_sentence == 1


def test_context_does_not_bridge_nonoverlapping_windows() -> None:
    source = thread("내일. 중간. 팀장님.")
    result = RuleRefiner(RefinerConfig(before=0, after=0)).select(source)
    assert len(result) == 2


def test_invalid_candidate_context_is_rejected() -> None:
    source = thread("내일 봐요.")
    candidate = RuleRefiner().select(source)[0]
    candidate.context = "변조"
    with pytest.raises(ValueError, match="context"):
        candidate.validate_thread(source)


@pytest.mark.parametrize("before,after", [(True, 1), (-1, 1), (1, 6), (1.5, 1)])
def test_direct_config_rejects_invalid_sizes(before: object, after: object) -> None:
    with pytest.raises(ValueError, match="refiner"):
        RefinerConfig(before=before, after=after)


def test_overlapping_segments_have_deterministic_provenance() -> None:
    source = thread("내일 봐요", parts=["내일 봐요"])
    overlap = source.segments[0].model_copy(update={"segment_id": "s2"})
    source.segments.append(overlap)
    selected = RuleRefiner().select(source)
    assert selected[0].segment_ids == ["s1", "s2"]
    assert selected[0].triggers[0].segment_ids == ["s1", "s2"]
    assert len(selected[0].triggers) == 1


def test_speakerless_audio_segments_and_thread_fallback() -> None:
    source = thread("인사. 내일 봐요. 종료.", parts=["인사.", "내일 봐요.", "종료."])
    source.channel = "call"
    for index, segment in enumerate(source.segments):
        segment.speaker = None
        segment.occurred_at = None
        segment.start_ms = index * 1000
        segment.end_ms = (index + 1) * 1000
    selected = RuleRefiner().select(source)
    assert selected[0].context == source.text
    assert selected[0].triggers[0].reference_at == STAMP
    assert source.segments[1].start_ms == 1000


def test_empty_lines_and_outer_whitespace_keep_offsets() -> None:
    source = thread("  인사!\n\n  내일 봐요?\n 종료  ")
    selected = RuleRefiner().select(source)
    assert selected[0].context == source.text[2:-2]
    assert selected[0].end_sentence == 3
    selected[0].validate_thread(source)


def test_trigger_serialization_is_sufficient_without_full_thread() -> None:
    source = thread("제외. 앞. 내일 만나요. 뒤. 비공개.")
    payload = [c.model_dump(mode="json") for c in RuleRefiner().select(source)]
    assert len(payload) == 1
    assert payload[0]["context"] == "앞. 내일 만나요. 뒤."
    assert payload[0]["triggers"][0]["reference_at"] == "2026-11-07T23:30:00+09:00"
    assert "비공개" not in str(payload)
    assert "제외" not in str(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_char", 999),
        ("sentence_index", 999),
        ("segment_ids", ["s9"]),
    ],
)
def test_invalid_trigger_provenance_is_rejected(field: str, value: object) -> None:
    source = thread("내일 봐요.")
    candidate = RuleRefiner().select(source)[0]
    setattr(candidate.triggers[0], field, value)
    with pytest.raises(ValueError, match="trigger"):
        candidate.validate_thread(source)


@pytest.mark.parametrize(
    "text,kind,expected",
    [
        ("이번 주 금요일", "date", ["이번 주", "금요일"]),
        ("오후 3시 30분", "time", ["오후 3시 30분"]),
        ("11월 8일", "date", ["11월 8일"]),
        ("두 시간", "duration", ["두 시간"]),
        ("홍길동 님", "honorific", ["홍길동 님"]),
    ],
)
def test_trigger_kinds_and_exact_original_ranges(
    text: str, kind: str, expected: list[str]
) -> None:
    source = thread(text)
    triggers = RuleRefiner().select(source)[0].triggers
    assert [t.kind for t in triggers] == [kind] * len(expected)
    assert [text[t.start_char : t.end_char] for t in triggers] == expected


@pytest.mark.parametrize(
    "text,signals",
    [
        ("3시로 옮기자", ["3시"]),
        ("금요일로", ["금요일"]),
        ("다음 주로", ["다음 주"]),
        ("내일엔", ["내일"]),
        ("내일이야", ["내일"]),
        ("내일이요", ["내일"]),
        ("내일부터는", ["내일"]),
        ("오후 3시예요", ["오후 3시"]),
        ("3시요", ["3시"]),
        ("팀장님이랑", ["팀장님"]),
        ("팀장님한테", ["팀장님"]),
        ("김민수님한테", ["김민수님"]),
        ("팀장님께서는", ["팀장님"]),
        ("철수씨랑", ["철수씨"]),
        ("홍길동 님이랑", ["홍길동 님"]),
        ("내일이랑 모레", ["내일", "모레"]),
        ("내일이나 모레", ["내일", "모레"]),
    ],
)
def test_korean_signal_suffixes(text: str, signals: list[str]) -> None:
    candidates = RuleRefiner().select(thread(text))
    assert [
        text[t.start_char : t.end_char] for c in candidates for t in c.triggers
    ] == signals


@pytest.mark.parametrize(
    "text",
    [
        "고객님",
        "회원님",
        "하느님",
        "마음씨",
        "아저씨",
        "아가씨",
        "오늘 씨 뿌렸다",
        "고객 님",
        "회원 님",
    ],
)
def test_common_nouns_and_spaced_ssi_are_not_honorifics(text: str) -> None:
    candidates = RuleRefiner().select(thread(text))
    assert all(t.kind != "honorific" for c in candidates for t in c.triggers)


@pytest.mark.parametrize(
    "text",
    [
        "오늘날",
        "내일모레",
        "내일림",
        "다음 주스",
        "일요일기",
        "3시스템",
        "오후 3시스템",
        "11월드",
        "김하나씨앗",
        "홍길동 님비",
    ],
)
def test_common_word_prefixes_do_not_trigger(text: str) -> None:
    assert RuleRefiner().select(thread(text)) == []


def test_date_final_period_and_version_boundaries() -> None:
    result = RuleRefiner(RefinerConfig(before=0, after=0)).select(
        thread("2026.11.8. 내일 봐요.")
    )
    assert [c.context for c in result] == ["2026.11.8.", "내일 봐요."]
    source = thread("버전 1.2.3 그대로. 내일 봐요.")
    result = RuleRefiner().select(source)
    assert (result[0].start_sentence, result[0].end_sentence) == (0, 2)
    assert result[0].context == source.text


@pytest.mark.parametrize("text", ["16:9 화면", "12:30:45", "3.5시간"])
def test_numeric_fragments_are_not_signals(text: str) -> None:
    assert RuleRefiner().select(thread(text)) == []


@pytest.mark.parametrize(
    "text,kind,signal",
    [
        ("15일에", "date", "15일"),
        ("1일", "date", "1일"),
        ("일주일", "duration", "일주일"),
        ("이틀", "duration", "이틀"),
        ("하루", "duration", "하루"),
        ("저녁 3시", "time", "저녁 3시"),
        ("새벽 3시", "time", "새벽 3시"),
        ("아침 3시", "time", "아침 3시"),
        ("밤 3시", "time", "밤 3시"),
    ],
)
def test_temporal_kinds_and_modifiers(text: str, kind: str, signal: str) -> None:
    triggers = RuleRefiner().select(thread(text))[0].triggers
    assert [(t.kind, text[t.start_char : t.end_char]) for t in triggers] == [
        (kind, signal)
    ]


def test_fullwidth_sentence_delimiters() -> None:
    result = RuleRefiner(RefinerConfig(before=0, after=0)).select(
        thread("내일。평범해요？팀장님！끝…")
    )
    assert [c.context for c in result] == ["내일。", "팀장님！"]
    assert [c.start_sentence for c in result] == [0, 2]


def test_uncovered_trigger_and_overlapping_null_segments_use_thread_reference() -> None:
    source = thread("내일 봐요. 팀장님!", parts=["내일 봐요."])
    source.segments[0].occurred_at = None
    source.segments.append(source.segments[0].model_copy(update={"segment_id": "s2"}))
    triggers = RuleRefiner().select(source)[0].triggers
    assert [t.segment_ids for t in triggers] == [["s1", "s2"], []]
    assert [t.reference_at for t in triggers] == [STAMP, STAMP]


@pytest.mark.parametrize("text", ["버전 1.2.3입니다. " * 8000, "3분 " * 35000])
def test_large_input_rule_selection_completes_promptly(text: str) -> None:
    from time import perf_counter

    assert len(text) >= 100_000
    start = perf_counter()
    selected = RuleRefiner().select(thread(text))
    elapsed = perf_counter() - start
    # Generous wall-clock cap catches the former quadratic scans (tens of
    # seconds on these inputs) while allowing slow development/CI machines.
    assert elapsed < 5.0
    if text.startswith("3분"):
        assert len(selected) == 1
        assert len(selected[0].triggers) == 35000
    else:
        assert selected == []
