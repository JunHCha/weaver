"""Call revision and summary share one LLM call; fake clients only."""

import hashlib
import json
import threading
from pathlib import Path

from pydantic import BaseModel

from weaver.context import ContextDictionary
from weaver.loom.summary import SummaryDrafts
from weaver.runtime.gate import LlmGate, LlmStatus
from weaver.runtime.summary import SummaryProcessor
from weaver.schema import Source
from weaver.schema.internal import ArchivedSource
from weaver.spinner import Spinner
from weaver.spinner.reviser import LlmReviser

SHORT = "나  (00:01)\n가짜 대화\n\n가상상대  (00:03)\n가짜 응답\n"
LONG = "나  (00:01)\n" + "가짜 대화 " * 40 + "\n\n가상상대  (00:03)\n가짜 응답\n"


def source(channel: str, text: str) -> Source:
    raw = json.loads(
        (Path(__file__).parents[2] / "schema/examples/Source.json").read_text()
    )
    raw.update(
        kind="text",
        channel=channel,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        hints={"counterpart": "가상상대"},
    )
    return Source.model_validate(raw)


class Store:
    def __init__(self, root: Path, src: Source, text: str) -> None:
        self.source = src
        self.path = root / "payload.txt"
        self.path.write_bytes(text.encode())

    def locate(self, source_id: str) -> ArchivedSource:
        return ArchivedSource(source=self.source, payload_path=self.path)


class Llm:
    model = "fake"

    def __init__(self, bad_segments: bool = False) -> None:
        self.tasks: list[str] = []
        self.bad_segments = bad_segments

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        self.tasks.append(task)
        payload = json.loads(user)
        segments = [
            {"segment_id": s["segment_id"], "text": s["text"] + "!"}
            for s in payload.get("segments", [])
        ]
        if task == "loom.summary":
            if "segments" in schema.model_fields:
                if self.bad_segments:
                    segments = [{"segment_id": "seg_missing", "text": "x"}]
                return schema.model_validate(
                    {
                        "script": "정리",
                        "body": "요약",
                        "questions": [],
                        "segments": segments,
                    }
                )
            return schema.model_validate(
                {"script": "정리", "body": "요약", "questions": []}
            )
        assert task == "spinner.revise"
        return schema.model_validate({"segments": segments})

    def generate_text(self, task: str, system: str, user: str) -> str:
        self.tasks.append(task)
        return "윤문"


def run(tmp_path: Path, channel: str, text: str, llm: Llm, threshold: int = 200):
    src = source(channel, text)
    spinner = Spinner(
        Store(tmp_path, src, text),
        tmp_path / "state",
        configuration_version="t",
        reviser=LlmReviser(llm),
        revision_threshold=threshold,
    )
    drafts = SummaryDrafts(
        llm,
        ContextDictionary(tmp_path / "state"),
        tmp_path / "proposals",
        state_root=tmp_path / "state",
        revision_threshold=threshold,
    )
    gate = LlmGate(lambda: LlmStatus(True, "ok"))
    outcome = SummaryProcessor(spinner, drafts, gate).process(src, threading.Event())
    assert outcome == "done"
    package = next(iter(drafts.proposals.glob("*.r1.json")))
    return src, spinner, json.loads(package.read_text())


def test_short_call_uses_one_call_and_preserves_corrected_script(tmp_path):
    llm = Llm()
    src, spinner, package = run(tmp_path, "call", SHORT, llm)
    assert llm.tasks == ["loom.summary"]
    assert "가짜 대화!" in package["source_text"]
    # The corrected Script is what Spinner serves for this source afterwards.
    assert spinner.spin(src).text == package["source_text"]


def test_long_call_uses_exactly_two_calls(tmp_path):
    llm = Llm()
    _, _, package = run(tmp_path, "call", LONG, llm)
    assert llm.tasks == ["spinner.revise", "loom.summary"]
    assert "가짜 응답!" in package["source_text"]


def test_text_channel_uses_one_call(tmp_path):
    llm = Llm()
    run(tmp_path, "share_text", "합성 메모", llm)
    assert llm.tasks == ["loom.summary"]


def test_invalid_revision_still_summarizes_with_original(tmp_path):
    llm = Llm(bad_segments=True)
    src, spinner, package = run(tmp_path, "call", SHORT, llm)
    assert llm.tasks == ["loom.summary"]
    assert package["source_text"] == SHORT
    assert package["draft_body"] == "요약"
    assert spinner.spin(src).text == SHORT
