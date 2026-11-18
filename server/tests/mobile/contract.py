"""Record request/response pairs of the phone API as shared contract fixtures.

``schema/mobile/fixtures/*.json`` are generated here and compared by
``test_fixtures.py``; the Android FakeMobileServer answers from the same files.
Volatile values (random IDs, tokens, "now" timestamps) are replaced by stable,
format-valid placeholders numbered by first appearance within each file.
"""

import json
import re
from pathlib import Path
from typing import Any

from .helpers import ROOT

FIXTURES = ROOT / "schema" / "mobile" / "fixtures"
ASSERT_HEADERS = (
    "etag",
    "location",
    "retry-after",
    "idempotent-replay",
    "www-authenticate",
)
SEND_HEADERS = ("if-match", "x-content-sha256", "content-type")
FIXED_TIME = "2026-11-19T00:00:00Z"
PLACEHOLDERS = [
    (re.compile(r"\bop_[0-9a-f]{32}\b"), lambda n: f"op_{n:032x}"),
    (re.compile(r"\bdev_[0-9a-f]{32}\b"), lambda n: f"dev_{n:032x}"),
    (re.compile(r"\bsrv_[0-9a-f]{32}\b"), lambda n: f"srv_{n:032x}"),
    (re.compile(r"\bep_[0-9a-f]{32}\b"), lambda n: f"ep_{n:032x}"),
    (re.compile(r"\bwvr_[A-Za-z0-9_-]{20,}\b"), lambda n: f"wvr_{n:043d}"),
    (re.compile(r"\bsrc_[0-7][0-9A-HJKMNP-TV-Z]{25}\b"), lambda n: f"src_{n:026d}"),
]


class Recorder:
    """Wraps a Harness client; every call becomes one named fixture case."""

    def __init__(self, harness: Any, group: str, description: str) -> None:
        self.h = harness
        self.group = group
        self.description = description
        self.cases: list[dict[str, Any]] = []

    def call(
        self,
        name: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        content: bytes | str | None = None,
        params: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> Any:
        sent = dict(headers or {})
        response = self.h.client.request(
            method, path, headers=sent, json=json_body, content=content, params=params
        )
        request: dict[str, Any] = {"method": method, "path": path}
        if params:
            request["query"] = {k: str(v) for k, v in params.items()}
        recorded = {
            k.lower(): ("Bearer <token>" if k.lower() == "authorization" else v)
            for k, v in sent.items()
            if k.lower() in (*SEND_HEADERS, "authorization")
        }
        if recorded:
            request["headers"] = recorded
        if json_body is not None:
            request["body"] = json_body
        elif content is not None:
            request["body_text"] = (
                content if isinstance(content, str) else content.decode()
            )
        reply: dict[str, Any] = {"status": response.status_code}
        kept = {k: v for k, v in response.headers.items() if k in ASSERT_HEADERS}
        if kept:
            reply["headers"] = kept
        if response.content:
            reply["body"] = response.json()
        case: dict[str, Any] = {"name": name, "request": request, "response": reply}
        if note:
            case["note"] = note
        self.cases.append(case)
        return response

    def document(self) -> dict[str, Any]:
        return normalize(
            {
                "contract_version": 1,
                "group": self.group,
                "description": self.description,
                "cases": self.cases,
            }
        )


def normalize(document: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(document, ensure_ascii=False, sort_keys=False)
    for pattern, make in PLACEHOLDERS:
        seen: dict[str, str] = {}
        for match in pattern.findall(text):
            seen.setdefault(match, make(len(seen) + 1))
        text = pattern.sub(lambda m, table=seen: table[m.group(0)], text)
    return stabilize(json.loads(text))


def stabilize(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: stabilize(v) for k, v in value.items()}
        if "operation_id" in out and "updated_at" in out:
            out["updated_at"] = FIXED_TIME
        if "received_at" in out:
            out["received_at"] = FIXED_TIME
        if "frontmatter" in out and "updated_at" in out:
            out["updated_at"] = FIXED_TIME
        return out
    if isinstance(value, list):
        return [stabilize(v) for v in value]
    return value


def dump(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def committed(group: str) -> Path:
    return FIXTURES / f"{group}.json"
