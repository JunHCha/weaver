"""A provider sink returns explicit valid fixtures after the real policy gate.

Responses are validated without filling missing fields or repairing invalid
examples; future negative cases can replace a task response directly.
"""

import json

from weaver.llm.errors import LlmError

TEXT = "검색 시안을 금요일에 검토하기로 했다."
AT = "2026-11-07T15:32:00+09:00"


class SyntheticLlm:
    def __init__(self):
        self.calls = []
        self.failures: dict[str, LlmError] = {}

    def generate_json(self, task, system, user, schema):
        payload = json.loads(user)
        self.calls.append((task, system, payload))
        if task in self.failures:
            raise self.failures[task]
        if task == "spinner.revise":
            data = {
                "segments": [
                    {"segment_id": row["segment_id"], "text": row["text"]}
                    for row in payload["segments"]
                ]
            }
        elif task == "loom.summary":
            data = {"script": payload["script"], "body": TEXT, "questions": []}
        elif task == "loom.approve":
            data = {
                "body": TEXT,
                "new_entries": [{"name": "검색 운영", "kind": "area", "aliases": []}],
                "corrections": [],
                "threads": [
                    {
                        "kind": "결정",
                        "content": TEXT,
                        "occurred_at": AT,
                        "tags": ["검색 운영"],
                        "field": None,
                        "value": None,
                        "direction": None,
                        "resolves": None,
                        "retracts": None,
                    }
                ],
            }
        elif task == "wiki.compile_batch":
            data = {
                "pages": [
                    {
                        "title": "검색 운영",
                        "role": "operation",
                        "summary": TEXT,
                        "thread_ids": [row["thread_id"] for row in payload["threads"]],
                        "state_tag": None,
                        "aliases": [],
                        "body": f"# 검색 운영\n\n{TEXT}\n",
                    }
                ],
                "excluded_threads": [],
                "digests": [
                    {
                        "path": target["path"],
                        "sentences": [{"text": TEXT, "evidence": target["evidence"]}],
                    }
                    for target in payload["digest_targets"]
                ],
            }
        else:
            raise AssertionError(f"Unexpected LLM task: {task}")
        return schema.model_validate(data)

    def generate_text(self, task, system, user):
        raise AssertionError(f"Unexpected unstructured LLM task: {task}")
