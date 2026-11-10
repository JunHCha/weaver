from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from weaver.context import ContextDictionary
from weaver.loom.summary import SummaryDrafts
from weaver.schema.v2 import Question, ReviewPackage


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
        llm, ContextDictionary(tmp_path / "state"), tmp_path / "proposals"
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
        revision_threshold=3,
    )
    service.create("src_01ARZ3NDEKTSV4RRFFQ69G5FAV", "긴 원문", package_id="pkg")
    assert [call[0] for call in llm.calls] == ["loom.polish", "loom.summary"]
    assert "긴 원문의 윤문" in llm.calls[1][1]
