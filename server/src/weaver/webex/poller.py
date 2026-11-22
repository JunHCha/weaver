"""Budgeted source polling; no attachment downloads and no Courier /changes cursor.

REST fixtures assume descending creation order and reply inclusion. These remain
pilot acceptance checks; an HTTP error or invalid page never proves deletion.
The next stage closes these durable B4 buffers into immutable capture requests.
"""

import hashlib
import math
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError

from weaver.shepherd.outbox import BundleKey, Outbox
from weaver.spinner.webex import WebexAttachment, WebexBundle, WebexMessage

from .settings import WebexSettings

API = "https://webexapis.com/v1"
CURSOR = "webex.poll.v1"
type Work = Literal["incremental", "overlap", "deletion"]


class Tokens(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


class PollError(ValueError):
    """Safe operator code, never a response body, URL, or private identifier."""


class Scan(BaseModel):
    floor: datetime
    ceiling: datetime
    url: str
    visited: list[str] = Field(default_factory=list)
    seen: set[str] = Field(default_factory=set)
    top: tuple[datetime, str] | None = None


class Seen(BaseModel):
    digest: str
    created: datetime


class Candidate(BaseModel):
    checked_at: datetime | None = None
    result: str = "unverified"


class Space(BaseModel):
    high_water: tuple[datetime, str] | None = None
    incremental: Scan | None = None
    overlap: Scan | None = None
    incremental_at: datetime | None = None
    overlap_at: datetime | None = None
    status: str = "ready"
    seen: dict[str, Seen] = Field(default_factory=dict)
    revisions: dict[str, int] = Field(default_factory=dict)
    candidates: dict[str, Candidate] = Field(default_factory=dict)


class State(BaseModel):
    spaces: dict[str, Space] = Field(default_factory=dict)
    rooms_url: str | None = None
    rooms_at: datetime | None = None
    rooms_visited: list[str] = Field(default_factory=list)
    turn_incremental: int = 0
    turn_overlap: int = 0
    window: int = -1
    used: dict[str, int] = Field(default_factory=dict)
    retry_at: datetime | None = None
    refresh_url: str | None = None
    failures: int = 0
    exclusions: set[str] = Field(default_factory=set)
    discovery_first: bool = True
    scheduled_window: int = -1


class Poller:
    """One serialized account worker; budgets and retry deadlines survive reboot."""

    def __init__(
        self,
        settings: WebexSettings,
        outbox: Outbox,
        tokens: Tokens,
        client: httpx.Client,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.settings, self.outbox, self.tokens, self.client = (
            settings,
            outbox,
            tokens,
            client,
        )
        self.clock, self.jitter = clock, jitter
        self.completed_overlap: set[str] = set()
        raw = outbox.cursor(CURSOR)
        self.state = State.model_validate_json(raw) if raw else State()

    def _save(self, buffers: list[tuple[BundleKey, bytes]] | None = None) -> None:
        self.outbox.buffer_page(
            buffers or [], cursor=(CURSOR, self.state.model_dump_json())
        )

    def _safe_url(self, url: str, path: str, space: str | None = None) -> str:
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            "https",
            "webexapis.com",
            path,
        ) or parsed.fragment:
            raise PollError("unsafe_pagination")
        if space is not None and parse_qs(parsed.query).get("roomId") != [space]:
            raise PollError("unsafe_pagination")
        return url

    def _reserve(self, work: Work) -> bool:
        window = int(self.clock().timestamp()) // self.settings.poll_interval_seconds
        if self.state.window != window:
            self.state.window, self.state.used = window, {}
        total = self.settings.request_budget_total
        cap = getattr(self.settings, f"request_budget_{work}")
        if (
            sum(self.state.used.values()) >= total
            or self.state.used.get(work, 0) >= cap
        ):
            return False
        self.state.used[work] = self.state.used.get(work, 0) + 1
        self._save()  # Count even requests interrupted before a response arrives.
        return True

    def _get(self, url: str, work: Work) -> httpx.Response | None:
        if self.state.retry_at is not None and self.clock() < self.state.retry_at:
            return None
        refresh = self.state.refresh_url == url
        while self._reserve(work):
            try:
                response = self.client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.tokens.access_token(force_refresh=refresh)}"
                    },
                    follow_redirects=False,
                )
            except httpx.TransportError:
                self._backoff(None)
                return None
            if response.status_code == 401:
                if refresh:
                    raise PollError("reauthentication_required")
                refresh = True
                self.state.refresh_url = url
                self._save()
                continue
            if response.status_code == 429 or response.status_code >= 500:
                self._backoff(response)
                return None
            self.state.refresh_url = None
            self.state.failures = 0
            self.state.retry_at = None
            return response
        return None

    def _backoff(self, response: httpx.Response | None) -> None:
        self.state.failures += 1
        delay = min(3600.0, 5.0 * 2 ** min(self.state.failures, 10))
        if response is not None and response.status_code == 429:
            value = response.headers.get("Retry-After", "")
            try:
                retry = float(value)
            except ValueError:
                try:
                    retry = (
                        parsedate_to_datetime(value) - self.clock()
                    ).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    retry = delay
            if math.isfinite(retry):
                delay = max(delay, min(retry, 365 * 86400.0))
        self.state.retry_at = self.clock() + timedelta(seconds=delay + self.jitter())
        self._save()

    @staticmethod
    def _items(response: httpx.Response) -> list[dict[str, object]]:
        if response.status_code != 200:
            raise PollError("webex_request_failed")
        try:
            raw = response.json()
            items = raw["items"]
            if not isinstance(items, list) or any(
                not isinstance(v, dict) for v in items
            ):
                raise ValueError
            return items
        except (ValueError, KeyError, TypeError):
            raise PollError("invalid_webex_page") from None

    def discover(self, *, one_page: bool = False) -> bool:
        now = self.clock()
        if self.state.rooms_url is None:
            if (
                self.state.rooms_at is not None
                and now
                < self.state.rooms_at
                + timedelta(seconds=self.settings.poll_interval_seconds)
            ):
                return True
            self.state.rooms_url = f"{API}/rooms?max=100"
            self.state.rooms_visited = []
        while self.state.rooms_url:
            url = self._safe_url(self.state.rooms_url, "/v1/rooms")
            response = self._get(url, "incremental")
            if response is None:
                return False
            items = self._items(response)
            ids = [item.get("id") for item in items]
            if any(not isinstance(sid, str) or not sid.strip() for sid in ids):
                raise PollError("invalid_webex_page")
            next_url = response.links.get("next", {}).get("url")
            if next_url:
                self._safe_url(next_url, "/v1/rooms")
                if next_url in [*self.state.rooms_visited, url]:
                    raise PollError("pagination_cycle")
            for sid in ids:
                if isinstance(sid, str) and sid not in self.settings.exclude_space_ids:
                    self.state.spaces.setdefault(sid, Space())
            self.state.rooms_visited.append(url)
            self.state.rooms_url = next_url
            if not next_url:
                self.state.rooms_at = now
            self._save()
            if one_page:
                return not next_url
        return True

    @staticmethod
    def _message(raw: dict[str, object], space: str) -> WebexMessage:
        try:
            if raw.get("roomId") != space:
                raise ValueError
            files = raw.get("files", [])
            if not isinstance(files, list) or any(
                not isinstance(v, str) or not v for v in files
            ):
                raise ValueError
            return WebexMessage.model_validate(
                {
                    "message_id": raw["id"],
                    "parent_id": raw.get("parentId"),
                    "author_display": raw.get("personDisplayName"),
                    "created_at": raw["created"],
                    "updated_at": raw.get("updated"),
                    "text": raw.get("text", ""),
                    "attachments": [WebexAttachment(url=v).model_dump() for v in files],
                }
            )
        except (KeyError, ValueError, TypeError, ValidationError):
            raise PollError("invalid_webex_message") from None

    def _buffers(
        self, sid: str, space: Space, messages: list[WebexMessage]
    ) -> list[tuple[BundleKey, bytes]]:
        grouped: dict[tuple[str, int], list[WebexMessage]] = {}
        for message in messages:
            digest = hashlib.sha256(message.model_dump_json().encode()).hexdigest()
            old = space.seen.get(message.message_id)
            if old is not None and old.digest == digest:
                continue
            day = (
                message.created_at.astimezone(ZoneInfo(self.settings.timezone))
                .date()
                .isoformat()
            )
            revision = space.revisions.get(day, 0)
            if old is not None:
                revision += 1  # Preserve previous observed bytes in an earlier buffer.
            key = BundleKey(self.outbox.instance_id, sid, day, 0, revision)
            try:
                entry = self.outbox.get(key.capture_id)
            except ValueError as error:
                if str(error) != "unknown_capture":
                    raise
                entry = None
            if entry is not None and entry.state != "buffer":
                revision += 1
            space.revisions[day] = revision
            grouped.setdefault((day, revision), []).append(message)
            space.seen[message.message_id] = Seen(
                digest=digest, created=message.created_at
            )
            space.candidates.pop(message.message_id, None)
        buffers: list[tuple[BundleKey, bytes]] = []
        for (day, revision), additions in grouped.items():
            key = BundleKey(self.outbox.instance_id, sid, day, 0, revision)
            try:
                existing = self.outbox.get(key.capture_id)
                bundle = WebexBundle.model_validate_json(existing.payload or b"")
                by_id = {m.message_id: m for m in bundle.messages}
            except ValueError as error:
                if str(error) != "unknown_capture":
                    raise
                by_id = {}
            by_id.update({m.message_id: m for m in additions})
            bundle = WebexBundle(
                format="weaver.webex.bundle.v1",
                instance_id=self.outbox.instance_id,
                space_id=sid,
                messages=sorted(
                    by_id.values(), key=lambda m: (m.created_at, m.message_id)
                ),
            )
            buffers.append((key, bundle.model_dump_json().encode()))
        return buffers

    def _page(self, sid: str, work: Literal["incremental", "overlap"]) -> bool:
        space = self.state.spaces[sid]
        scan = getattr(space, work)
        now = self.clock()
        if scan is None:
            finished = (
                space.incremental_at if work == "incremental" else space.overlap_at
            )
            if finished is not None and now < finished + timedelta(
                seconds=self.settings.poll_interval_seconds
            ):
                return False
            floor = (
                space.high_water[0]
                if work == "incremental" and space.high_water
                else now
                - timedelta(
                    days=self.settings.initial_backfill_days
                    if work == "incremental"
                    else self.settings.reconcile_overlap_days
                )
            )
            scan = Scan(
                floor=floor,
                ceiling=now,
                url=f"{API}/messages?"
                + urlencode({"roomId": sid, "before": now.isoformat(), "max": 100}),
            )
            setattr(space, work, scan)
        url = self._safe_url(scan.url, "/v1/messages", sid)
        response = self._get(url, work)
        if response is None:
            return False
        if response.status_code in (403, 404):
            space.status = "access_gap"
            self._save()
            return False
        messages = [self._message(raw, sid) for raw in self._items(response)]
        if any(
            a.created_at < b.created_at
            for a, b in zip(messages, messages[1:], strict=False)
        ):
            raise PollError("unexpected_message_order")
        if any(m.created_at >= scan.ceiling for m in messages):
            raise PollError("unexpected_message_range")
        next_url = response.links.get("next", {}).get("url")
        if next_url:
            self._safe_url(next_url, "/v1/messages", sid)
            if next_url in [*scan.visited, url]:
                raise PollError("pagination_cycle")
        selected = [m for m in messages if m.created_at >= scan.floor]
        for message in selected:
            scan.seen.add(message.message_id)
            top = (message.created_at, message.message_id)
            if scan.top is None or top > scan.top:
                scan.top = top
        done = not next_url or any(m.created_at < scan.floor for m in messages)
        buffers = self._buffers(sid, space, selected)
        if done:
            if work == "incremental":
                if scan.top is not None:
                    space.high_water = max(space.high_water or scan.top, scan.top)
                space.incremental_at = now
            else:
                for mid, seen in space.seen.items():
                    if (
                        scan.floor <= seen.created < scan.ceiling
                        and mid not in scan.seen
                    ):
                        space.candidates.setdefault(mid, Candidate())
                space.overlap_at = now
                self.completed_overlap.add(sid)
            setattr(space, work, None)
            space.status = "ready"
        else:
            scan.visited.append(url)
            scan.url = next_url
        self._save(
            buffers
        )  # high-water + pagination + observed bodies: one B4 transaction
        return True

    def _round(self, work: Literal["incremental", "overlap"]) -> None:
        ids = sorted(
            sid
            for sid, space in self.state.spaces.items()
            if sid not in self.settings.exclude_space_ids
            and space.status != "access_gap"
        )
        if not ids:
            return
        attribute = f"turn_{work}"
        idle = 0
        while idle < len(ids):
            turn = getattr(self.state, attribute) % len(ids)
            setattr(self.state, attribute, turn + 1)
            if self._page(ids[turn], work):
                idle = 0
            else:
                idle += 1
        self._save()

    def _deletions(self) -> None:
        # A fresh successful overlap proves access only for that run. A missing
        # single message remains unverified (retention/revocation is ambiguous).
        for sid, space in sorted(self.state.spaces.items()):
            if (
                sid in self.settings.exclude_space_ids
                or sid not in self.completed_overlap
                or space.status != "ready"
            ):
                continue
            for mid, candidate in space.candidates.items():
                response = self._get(
                    f"{API}/messages/{quote(mid, safe='')}", "deletion"
                )
                if response is None:
                    return
                candidate.checked_at = self.clock()
                candidate.result = (
                    "present" if response.status_code == 200 else "unverified"
                )
                self._save()

    def tick(self) -> None:
        """Incremental has first claim; overlap is a separate resumable round."""
        # Discard in-memory mutations after any failed transaction/parse so that
        # the same Poller object is as safe to retry as a restarted process.
        try:
            self.completed_overlap.clear()
            excluded = set(self.settings.exclude_space_ids)
            for sid in self.state.exclusions - excluded:
                if sid in self.state.spaces:
                    space = self.state.spaces[sid]
                    space.high_water = None
                    space.incremental = space.overlap = None
                    space.incremental_at = space.overlap_at = None
                    space.status = "ready"
            self.state.exclusions = excluded
            self._save()
            window = (
                int(self.clock().timestamp()) // self.settings.poll_interval_seconds
            )
            discovery_first = self.state.discovery_first
            if window != self.state.scheduled_window:
                self.state.scheduled_window = window
                self.state.discovery_first = not discovery_first
            if discovery_first or not self.state.spaces:
                self.discover(one_page=True)
            self._round("incremental")
            self.discover(one_page=True)
            self._round("incremental")
            self._round("overlap")
            self._deletions()
        finally:
            raw = self.outbox.cursor(CURSOR)
            self.state = State.model_validate_json(raw) if raw else State()
