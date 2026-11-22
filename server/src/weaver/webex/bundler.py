"""Pilot stage 7: close Space-day buffers into immutable, linked Source requests.

A day closes only after a later successful poll proved nothing before the local
midnight was missed. Closing is one durable Outbox commit per revision:
deterministic message-boundary parts (64 MiB payload limit), frozen payload and
Manifest bytes and, for a correction, the capture it depends on. Excluded Spaces
are never closed. Nothing here touches the network or an approved Thread.
"""

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from weaver.schema import Manifest
from weaver.shepherd.outbox import BundleKey, Outbox
from weaver.spinner.webex import WebexBundle, WebexMessage

MAX_PAYLOAD_BYTES = 64 * 1024**2
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class MessageTooLarge(ValueError):
    """One message cannot fit a part: keep the buffer and ask for human action."""


@dataclass
class CloseResult:
    closed: list[str] = field(default_factory=list)
    # (space, day, revision) -> fixed operator code; the buffer bytes stay intact.
    held: dict[tuple[str, str, int], str] = field(default_factory=dict)


def _ordered(messages: list[WebexMessage]) -> list[WebexMessage]:
    return sorted(messages, key=lambda m: (m.created_at.astimezone(UTC), m.message_id))


def _size(message: WebexMessage) -> int:
    return len(message.model_dump_json().encode())


def _bundle(
    template: WebexBundle,
    messages: list[WebexMessage],
    *,
    day: str,
    part: int,
    count: int,
    revision: int,
    corrects: str | None,
) -> WebexBundle:
    return WebexBundle(
        format=template.format,
        instance_id=template.instance_id,
        space_id=template.space_id,
        messages=messages,
        day=day,
        part=part,
        part_count=count,
        revision=revision,
        corrects_capture_id=corrects,
    )


def split_parts(
    bundle: WebexBundle, day: str, limit: int = MAX_PAYLOAD_BYTES
) -> list[list[WebexMessage]]:
    """Greedy, order-stable split at message boundaries; same input, same parts."""
    ordered = _ordered(bundle.messages)
    # Worst-case envelope (huge part numbers, a correction link) bounds the rest.
    probe = _bundle(
        bundle,
        ordered[:1],
        day=day,
        part=10**6,
        count=10**6,
        revision=10**6,
        corrects="cap_" + "0" * 26,
    )
    envelope = len(probe.model_dump_json().encode()) - _size(ordered[0])
    chunks: list[list[WebexMessage]] = []
    current: list[WebexMessage] = []
    used = envelope
    for message in ordered:
        size = _size(message)
        if envelope + size > limit:
            raise MessageTooLarge(message.message_id)
        extra = size + (1 if current else 0)  # JSON array comma
        if current and used + extra > limit:
            chunks.append(current)
            current, used, extra = [], envelope, size
        current.append(message)
        used += extra
    chunks.append(current)
    return chunks


def _manifest(key: BundleKey, body: bytes, tz: ZoneInfo, now: datetime) -> Manifest:
    occurred = datetime.combine(date.fromisoformat(key.day), time.min, tzinfo=tz)
    return Manifest.model_validate(
        {
            "schema_version": 1,
            "capture_id": key.capture_id,
            "capture_host": "mac",
            "kind": "text",
            "channel": "webex",
            "payload": key.capture_id + ".txt",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "captured_at": now.astimezone(UTC).isoformat(),
            "occurred_at": occurred.isoformat(),
            "hints": {"counterpart": None},
        }
    )


def _freeze(
    outbox: Outbox,
    key: BundleKey,
    tz: ZoneInfo,
    now: datetime,
    limit: int,
) -> list[str]:
    payload = outbox.get(key.capture_id).payload or b""
    bundle = WebexBundle.model_validate_json(payload)
    chunks = split_parts(bundle, key.day, limit)
    parts: list[tuple[BundleKey, bytes, Manifest, str | None]] = []
    for number, chunk in enumerate(chunks):
        part_key = BundleKey(key.instance_id, key.space, key.day, number, key.revision)
        parent = outbox.correction_parent(part_key)
        if key.revision > 0 and parent is None:
            raise ValueError("missing_correction_parent")
        body = (
            _bundle(
                bundle,
                chunk,
                day=key.day,
                part=number,
                count=len(chunks),
                revision=key.revision,
                corrects=parent,
            )
            .model_dump_json()
            .encode()
        )
        if len(body) > limit:
            raise ValueError("part_too_large")
        parts.append((part_key, body, _manifest(part_key, body, tz, now), parent))
    outbox.freeze_parts(key, payload, parts)
    return [part_key.capture_id for part_key, *_ in parts]


def close_ready(
    outbox: Outbox,
    *,
    timezone: str,
    now: datetime,
    eligible: Callable[[str, datetime], bool],
    max_payload_bytes: int = MAX_PAYLOAD_BYTES,
) -> CloseResult:
    """Freeze every open buffer whose local day ended and whose Space is synced.

    `eligible(space, day_end)` is the poller's proof that the Space completed a
    successful incremental poll covering `day_end`. An excluded Space is left
    untouched (bytes preserved, nothing held). A buffer that cannot close is
    held with a fixed code and blocks only the later revisions of its own day.
    """
    tz = ZoneInfo(timezone)
    groups: dict[tuple[str, str], list[BundleKey]] = defaultdict(list)
    for key, state in outbox.bundle_keys():
        if (
            state == "buffer"
            and key.part == 0
            and key.instance_id == outbox.instance_id
        ):
            groups[(key.space, key.day)].append(key)
    result = CloseResult()
    for (space, day), keys in sorted(groups.items()):
        end = datetime.combine(
            date.fromisoformat(day) + timedelta(days=1), time.min, tzinfo=tz
        )
        if outbox.space_excluded(space) or not eligible(space, end):
            continue
        for key in sorted(keys, key=lambda k: k.revision):
            try:
                result.closed.extend(_freeze(outbox, key, tz, now, max_payload_bytes))
            except MessageTooLarge:
                outbox.notify("webex_message_too_large")
                result.held[(space, day, key.revision)] = "message_too_large"
                break
            except ValueError as error:
                if str(error) == "excluded_space":
                    break
                code = str(error)
                result.held[(space, day, key.revision)] = (
                    code if _SAFE_CODE.fullmatch(code) else "invalid_buffer"
                )
                break
    return result
