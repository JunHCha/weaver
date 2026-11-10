"""LlmClient test doubles for evaluation: fixture replay and call tracing."""

import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from weaver.llm import InvalidJson, SchemaMismatch
from weaver.llm.types import LlmClient


def digest(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ReplayClient:
    """Versioned, independent provider responses; no expected-answer lookup."""

    model = "fixture.v1"

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        response = self.responses[task]
        if isinstance(response, dict) and "by_input" in response:
            response = response["by_input"][digest(json.loads(user))]
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                raise InvalidJson() from None
        try:
            return schema.model_validate(response)
        except ValidationError:
            raise SchemaMismatch() from None

    def generate_text(self, task: str, system: str, user: str) -> str:
        raise ValueError("unexpected text request")


class TracedClient:
    """Record each structured call's task, prompt hash, status and latency."""

    def __init__(self, client: LlmClient) -> None:
        self.client = client
        self.model = str(getattr(client, "model", "unknown"))
        self.calls: list[dict[str, Any]] = []

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        started = time.perf_counter()
        record: dict[str, Any] = {
            "task": task,
            "prompt_hash": digest([system, user, schema.model_json_schema()]),
            "cost_usd": 0 if isinstance(self.client, ReplayClient) else None,
        }
        self.calls.append(record)
        try:
            result = self.client.generate_json(task, system, user, schema)
            record["status"] = "succeeded"
            return result
        except Exception as error:
            record["status"] = type(error).__name__
            raise
        finally:
            record["elapsed_seconds"] = time.perf_counter() - started

    def generate_text(self, task: str, system: str, user: str) -> str:
        return self.client.generate_text(task, system, user)
