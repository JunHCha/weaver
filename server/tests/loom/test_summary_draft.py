import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from weaver.context import ContextDictionary
from weaver.loom.call import GalaxyCallParser
from weaver.loom.summary import SummaryDrafts
from weaver.schema import Question, ReviewPackage, Script, Source


class FakeLlm:
    def __init__(self, questions: int) -> None:
        self.questions = questions
        self.calls: list[tuple[str, str]] = []

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        self.calls.append((task, user))
        return schema.model_validate(
            {
                "script": "정리한 원문",
                "body": "# 요약\n결정했다.",
                "questions": [
                    {
                        "question": f"질문 {n}",
                        "rationale": "불확실",
                        "choices": ["하나", "둘", "셋"],
                    }
                    for n in range(self.questions)
                ],
            }
        )

    def generate_text(self, task: str, system: str, user: str) -> str:
        self.calls.append((task, user))
        return "긴 원문의 윤문"


def test_draft_limits_questions_and_publishes(tmp_path: Path) -> None:
    llm = FakeLlm(4)
    service = SummaryDrafts(
        llm,
        ContextDictionary(tmp_path / "state"),
        tmp_path / "proposals",
        state_root=tmp_path / "state",
    )
    package, script = service.create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "원본",
        "홍길동",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        package_id="pkg",
    )
    assert script == "정리한 원문"
    assert len(package.questions) == 3
    assert "확인 필요" in package.draft_body
    assert package.stage == "검토"
    assert "홍길동" in llm.calls[0][1]
    assert len(llm.calls) == 1
    assert (
        ReviewPackage.model_validate_json(
            (tmp_path / "proposals" / "pkg.r1.json").read_text()
        )
        == package
    )


def test_response_requires_three_choices() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Question(question="질문", rationale="근거", choices=["하나"])


def test_long_script_has_separate_revision_pass(tmp_path: Path) -> None:
    llm = FakeLlm(0)
    service = SummaryDrafts(
        llm,
        ContextDictionary(tmp_path / "state"),
        tmp_path / "proposals",
        state_root=tmp_path / "state",
        revision_threshold=3,
    )
    service.create("src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "긴 원문", package_id="pkg")
    assert [call[0] for call in llm.calls] == ["loom.polish", "loom.summary"]
    assert "긴 원문의 윤문" in llm.calls[1][1]


def test_draft_is_available_after_restart(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    service = SummaryDrafts(
        FakeLlm(0),
        ContextDictionary(state_root),
        tmp_path / "proposals",
        state_root=state_root,
    )
    package, script = service.create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "원본", package_id="pkg"
    )
    restored = SummaryDrafts(
        FakeLlm(0),
        ContextDictionary(state_root),
        tmp_path / "proposals",
        state_root=state_root,
    )
    assert restored.state.load_package("pkg", 1) == (package, script)


def test_original_source_and_topic_survive_polish(tmp_path: Path) -> None:
    llm = FakeLlm(1)
    service = SummaryDrafts(
        llm,
        ContextDictionary(tmp_path / "state"),
        tmp_path / "proposals",
        state_root=tmp_path / "state",
        revision_threshold=3,
    )
    original = "첫 통화 원문입니다.\n질문 0과 관련한 내용입니다."
    package, _ = service.create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", original, source_type="call"
    )
    assert package.source_text == original
    assert package.source_type == "call"
    assert package.title == "결정했다."
    assert package.questions[0].source_excerpt in original
    assert "질문 0" in package.questions[0].source_excerpt
    assert [call[0] for call in llm.calls] == ["loom.polish", "loom.summary"]


CALL_TEXT = "나  (00:01)\n가짜 대화\n\n가상상대  (00:03)\n가짜 응답\n"


class CallLlm:
    """Single-pass fake: returns segment edits next to the summary output."""

    def __init__(self, edits: list[dict[str, str]] | None = None) -> None:
        self.edits = edits
        self.calls: list[tuple[str, dict]] = []

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        payload = json.loads(user)
        self.calls.append((task, payload))
        edits = self.edits
        if edits is None:
            first = payload["segments"][0]
            edits = [{"segment_id": first["segment_id"], "text": "교정된 대화"}]
        return schema.model_validate(
            {
                "script": "정리한 원문",
                "body": "# 요약\n결정했다.",
                "title": "제목",
                "questions": [],
                "segments": edits,
            }
        )

    def generate_text(self, task: str, system: str, user: str) -> str:
        self.calls.append((task, {}))
        return "긴 원문의 윤문"


def call_script() -> Script:
    source = Source.model_validate(
        {
            **json.loads(
                (Path(__file__).parents[3] / "schema/examples/Source.json").read_text()
            ),
            "kind": "text",
            "channel": "call",
        }
    )
    return GalaxyCallParser().parse(source, CALL_TEXT)


def drafts(tmp_path: Path, llm, threshold: int = 30000) -> SummaryDrafts:
    return SummaryDrafts(
        llm,
        ContextDictionary(tmp_path / "state"),
        tmp_path / "proposals",
        state_root=tmp_path / "state",
        revision_threshold=threshold,
    )


def test_call_revision_and_summary_share_one_call(tmp_path: Path) -> None:
    llm = CallLlm()
    script = call_script()
    saved: list[Script] = []
    package, _ = drafts(tmp_path, llm).create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        script.text,
        source_type="call",
        call=script,
        on_revised=saved.append,
        package_id="pkg",
    )
    assert [task for task, _ in llm.calls] == ["loom.summary"]
    sent = llm.calls[0][1]["segments"]
    assert [s["text"] for s in sent] == ["가짜 대화", "가짜 응답"]
    assert "교정된 대화" in package.source_text
    assert package.source_text == saved[0].text
    assert package.title == "제목"


def test_call_revision_failure_keeps_source_and_summary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    llm = CallLlm([{"segment_id": "seg_unknown", "text": "비밀 본문 문장"}])
    script = call_script()
    saved: list[Script] = []
    with caplog.at_level(logging.WARNING):
        package, _ = drafts(tmp_path, llm).create(
            "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            script.text,
            source_type="call",
            call=script,
            on_revised=saved.append,
            package_id="pkg",
        )
    assert [task for task, _ in llm.calls] == ["loom.summary"]
    assert saved == [] and package.source_text == script.text
    assert package.draft_body == "# 요약\n결정했다."
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "비밀 본문 문장" not in caplog.text and "가짜 대화" not in caplog.text


@pytest.mark.parametrize(
    "edits",
    [
        [{"segment_id": "a", "text": "x"}, {"segment_id": "a", "text": "y"}],
        [{"segment_id": "{first}", "text": "  "}],
    ],
)
def test_call_revision_rejects_duplicates_and_blank(tmp_path: Path, edits) -> None:
    script = call_script()
    first = next(s.segment_id for s in script.segments if s.speaker)
    edits = [
        {**e, "segment_id": e["segment_id"].replace("{first}", first)} for e in edits
    ]
    edits = [
        {**e, "segment_id": first if e["segment_id"] == "a" else e["segment_id"]}
        for e in edits
    ]
    saved: list[Script] = []
    package, _ = drafts(tmp_path, CallLlm(edits)).create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        script.text,
        call=script,
        on_revised=saved.append,
        package_id="pkg",
    )
    assert saved == [] and package.source_text == script.text


def test_already_revised_long_script_skips_polish(tmp_path: Path) -> None:
    llm = FakeLlm(0)
    drafts(tmp_path, llm, threshold=3).create(
        "src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "긴 원문", revised=True, package_id="pkg"
    )
    assert [call[0] for call in llm.calls] == ["loom.summary"]
