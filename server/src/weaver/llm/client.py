"""Synchronous LlmClient with a cancellable, wall-clock bounded SDK request.

The async SDK is an internal implementation detail: a fresh event loop on a
joined worker thread also allows synchronous callers inside an existing loop.
No background paid request survives the return from a public method.
"""

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast

import anthropic
from pydantic import BaseModel, ValidationError

from weaver.config import LlmConfig

from .anthropic import AnthropicTransport
from .errors import (
    AuthenticationError,
    InvalidJson,
    LlmError,
    LlmTimeout,
    MissingOutput,
    RateLimited,
    Refusal,
    SchemaMismatch,
    TransientError,
    UnsupportedStructuredOutput,
)
from .ledger import NANO_USD, Ledger
from .types import BackendReply, Request, Transport

MODEL = "claude-sonnet-5-5"


class AnthropicClient:
    def __init__(
        self,
        config: LlmConfig,
        ledger: Ledger,
        *,
        transport: Transport | None = None,
        environ: Mapping[str, str] | None = None,
        max_tokens: int = 16384,
        deadline_seconds: float = 120,
        structured_output: bool = True,
    ) -> None:
        if config.backend != "anthropic":
            raise ValueError("AnthropicClient requires anthropic backend")
        if not 1 <= max_tokens <= 64000 or not 0 < deadline_seconds <= 120:
            raise ValueError("invalid output limit or deadline")
        self.config = config
        self.ledger = ledger
        self.transport = transport if transport is not None else AnthropicTransport()
        self.environ = os.environ if environ is None else environ
        self.max_tokens = max_tokens
        self.deadline_seconds = deadline_seconds
        self.structured_output = structured_output

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        return cast(T, self._generate(task, system, user, schema))

    def generate_text(self, task: str, system: str, user: str) -> str:
        return cast(str, self._generate(task, system, user, None))

    def _generate[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T] | None
    ) -> Any:
        if (
            re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", task) is None
            or len(task) > 80
        ):
            raise ValueError("task must be a short machine identifier")
        started = time.monotonic()
        record: dict[str, Any] = {
            "task": task,
            "backend": "anthropic",
            "model": MODEL,
            "started_at": datetime.now(UTC).isoformat(),
            "price_snapshot": "2026-11-07.standard-global",
            "planned_calls_per_day": 3,
            "status": "pending",
            "elapsed_seconds": 0.0,
            "attempts": 0,
            "usage": [],
            "cost_usd": None,
            "cost_source": "unavailable",
            "attempt_records": [],
        }
        # The SDK transform removes unsupported constraints. Always validate
        # the final reply against the original Pydantic model as well.
        try:
            if schema is not None and not self.structured_output:
                raise UnsupportedStructuredOutput()
            reference = self.config.api_key_env
            api_key = self.environ.get(reference, "") if reference else ""
            if not api_key:
                raise AuthenticationError()
            json_schema = (
                anthropic.transform_schema(schema) if schema is not None else None
            )
        except AuthenticationError:
            record["status"] = AuthenticationError.code
            record["elapsed_seconds"] = time.monotonic() - started
            self.ledger.reserve(record, 0)
            raise
        except Exception:
            record["status"] = UnsupportedStructuredOutput.code
            record["elapsed_seconds"] = time.monotonic() - started
            self.ledger.reserve(record, 0)
            raise UnsupportedStructuredOutput() from None
        request = Request(MODEL, system, user, self.max_tokens, json_schema)
        # A UTF-8 byte upper bound plus conservative envelope overhead, never
        # the research table's character/token average. No tools/cache/history.
        # JSON escaping is included; use the exact transformed wire schema.
        wire_bytes = len(
            json.dumps(
                {
                    "system": system,
                    "user": user,
                    "schema": json_schema,
                },
                ensure_ascii=True,
            ).encode("utf-8")
        )
        per_attempt = (wire_bytes + 8192) * 2000 + self.max_tokens * 10000
        call_id = self.ledger.reserve(record, per_attempt * 2)
        charge = 0
        known_cost = 0
        unknown_cost = False
        try:

            async def run() -> BackendReply:
                nonlocal charge, known_cost, unknown_cost
                # Preparation and reservation consume the same deadline;
                # never dispatch if they already used the available time.
                remaining = self.deadline_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise LlmTimeout()
                # Includes transport setup, both requests, backoff and cleanup.
                async with asyncio.timeout(remaining):
                    for attempt in range(2):
                        record["attempts"] = attempt + 1
                        # Until a billed response proves otherwise this attempt
                        # consumes its reservation, including cancellation.
                        charge += per_attempt
                        attempt_record: dict[str, Any] = {
                            "status": "pending",
                            "usage": None,
                            "cost_usd": None,
                            "cost_source": "unavailable",
                        }
                        record["attempt_records"].append(attempt_record)
                        try:
                            reply = await self.transport.send(request, api_key)
                        except RateLimited:
                            charge -= per_attempt  # rejected, no generation
                            attempt_record["status"] = RateLimited.code
                            if attempt == 1:
                                raise
                        except TransientError:
                            unknown_cost = True
                            attempt_record["status"] = TransientError.code
                            if attempt == 1:
                                raise
                        except asyncio.CancelledError:
                            attempt_record["status"] = LlmTimeout.code
                            raise
                        except LlmError as exc:
                            attempt_record["status"] = exc.code
                            raise
                        except Exception:
                            attempt_record["status"] = LlmError.code
                            raise
                        else:
                            if reply.usage is not None:
                                cost = reply.usage.cost_nano_usd
                                charge += cost - per_attempt
                                known_cost += cost
                                record["usage"].append(asdict(reply.usage))
                                attempt_record.update(
                                    {
                                        "usage": asdict(reply.usage),
                                        "cost_usd": cost / NANO_USD,
                                        "cost_source": "usage_calculated",
                                    }
                                )
                            else:
                                unknown_cost = True
                            attempt_record["status"] = reply.stop_reason
                            return reply
                        await asyncio.sleep(0.25)
                raise LlmTimeout()

            # Joined before returning: asyncio.timeout cancels in-flight I/O,
            # SDK async context closes the HTTP client on cancellation too.
            with ThreadPoolExecutor(max_workers=1) as executor:
                reply = executor.submit(lambda: asyncio.run(run())).result()
            if reply.stop_reason == "refusal":
                raise Refusal()
            if reply.stop_reason != "end_turn" or not reply.text:
                raise MissingOutput()
            if schema is None:
                result: Any = reply.text
            else:
                try:
                    json.loads(reply.text)
                except (ValueError, RecursionError):
                    raise InvalidJson() from None
                try:
                    result = schema.model_validate_json(reply.text)
                except ValidationError:
                    raise SchemaMismatch() from None
            record["status"] = "succeeded"
            return result
        except (TimeoutError, LlmTimeout):
            unknown_cost = True
            record["status"] = LlmTimeout.code
            raise LlmTimeout() from None
        except AuthenticationError:
            # A provider authentication rejection cannot incur generation cost.
            if record["attempts"]:
                charge -= per_attempt
            record["status"] = AuthenticationError.code
            raise
        except LlmError as exc:
            record["status"] = exc.code
            if exc.code == LlmError.code:
                unknown_cost = True
            raise
        except Exception:
            unknown_cost = True
            record["status"] = LlmError.code
            raise LlmError() from None
        finally:
            record["elapsed_seconds"] = time.monotonic() - started
            if record["attempts"] and not unknown_cost and record["usage"]:
                record["cost_usd"] = known_cost / NANO_USD
                record["cost_source"] = "usage_calculated"
            self.ledger.finish(call_id, record, charge)
