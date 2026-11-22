"""Budgeted source polling; no attachment downloads and no Courier /changes cursor.

REST fixtures assume descending creation order and reply inclusion. These remain
pilot acceptance checks; an HTTP error or invalid page never proves deletion.
Closing the durable B4 buffers into immutable capture requests (stage 7) lives in
bundler.py and runs at the end of each tick.
"""

import hashlib
import math
import random
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError

from weaver.shepherd.outbox import MAX_PAYLOAD_BYTES, BundleKey, CapacityError, Outbox
from weaver.spinner.webex import WebexAttachment, WebexBundle, WebexMessage

from .bundler import close_ready
from .settings import WebexSettings

API = "https://webexapis.com/v1"
CURSOR = "webex.poll.v1"
BUDGET = "webex.budget.v1"
type Work = Literal["incremental", "overlap", "deletion"]


def _split_hold(slot: str) -> tuple[str, int]:
    day, _, revision = slot.partition("#")
    return day, int(revision or 0)


class Tokens(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


class PollError(ValueError):
    """Safe operator code, never a response body, URL, or private identifier."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.status = status


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
    missing_at: datetime | None = None
    checked_at: datetime | None = None
    result: str = "unverified"


class Space(BaseModel):
    high_water: tuple[datetime, str] | None = None
    incremental: Scan | None = None
    overlap: Scan | None = None
    incremental_at: datetime | None = None
    overlap_at: datetime | None = None
    # Upper bound of the last completed incremental scan: every message created
    # before it was observed. Stage 7 closes a day only once this passes midnight.
    synced_through: datetime | None = None
    # "day#revision" -> fixed code for buffers stage 7 could not close.
    holds: dict[str, str] = Field(default_factory=dict)
    # "day" -> why a finished local day was not closed (Space not ready yet).
    close_waiting: dict[str, str] = Field(default_factory=dict)
    status: str = "ready"
    gap_since: datetime | None = None
    last_status: int | None = None
    reason: str | None = None
    not_found_count: int = 0
    retry_at: datetime | None = None
    excluded: bool = False
    seen: dict[str, Seen] = Field(default_factory=dict)
    revisions: dict[str, int] = Field(default_factory=dict)
    candidates: dict[str, Candidate] = Field(default_factory=dict)
    observations: dict[str, Candidate] = Field(default_factory=dict)


class RequestBudget(BaseModel):
    attempts: list[tuple[datetime, Work]] = Field(default_factory=list)
    last_at: datetime | None = None

    @property
    def used(self) -> dict[str, int]:
        return {
            work: sum(w == work for _, w in self.attempts)
            for work in ("incremental", "overlap", "deletion")
            if any(w == work for _, w in self.attempts)
        }


class State(BaseModel):
    spaces: dict[str, Space] = Field(default_factory=dict)
    rooms_url: str | None = None
    rooms_at: datetime | None = None
    rooms_retry_at: datetime | None = None
    rooms_error: str | None = None
    rooms_visited: list[str] = Field(default_factory=list)
    turn_incremental: int = 0
    turn_overlap: int = 0
    stopped: str | None = None
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
        max_payload_bytes: int = MAX_PAYLOAD_BYTES,
    ) -> None:
        self.max_payload_bytes = max_payload_bytes
        self.settings, self.outbox, self.tokens, self.client = (
            settings,
            outbox,
            tokens,
            client,
        )
        self.clock, self.jitter = clock, jitter
        self.accessible_spaces: set[str] = set()
        raw = outbox.cursor(CURSOR)
        self.state = State.model_validate_json(raw) if raw else State()
        raw_budget = outbox.cursor(BUDGET)
        self.budget = (
            RequestBudget.model_validate_json(raw_budget)
            if raw_budget
            else RequestBudget()
        )
        if raw and not raw_budget:
            # Conservatively carry old fixed-window counters into a full new window.
            import json

            for work, count in json.loads(raw).get("used", {}).items():
                if work in ("incremental", "overlap", "deletion"):
                    self.budget.attempts.extend((clock(), work) for _ in range(count))
            self._save_budget()

    def _save_budget(self) -> None:
        self.outbox.buffer_page([], cursor=(BUDGET, self.budget.model_dump_json()))

    def _save(self, buffers: list[tuple[BundleKey, bytes]] | None = None) -> None:
        self.outbox.buffer_page(
            buffers or [],
            cursor=(CURSOR, self.state.model_dump_json()),
            exclusions=tuple(sorted(self.state.exclusions)),
        )

    def _safe_url(self, url: str, path: str, space: str | None = None) -> str:
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise PollError("unsafe_pagination") from None
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
        now = max(self.clock(), self.budget.last_at or self.clock())
        self.budget.last_at = now
        floor = now - timedelta(seconds=self.settings.request_budget_window_seconds)
        self.budget.attempts = [(at, w) for at, w in self.budget.attempts if at > floor]
        total = self.settings.request_budget_total
        cap = getattr(self.settings, f"request_budget_{work}")
        if len(self.budget.attempts) >= total or self.budget.used.get(work, 0) >= cap:
            return False
        self.budget.attempts.append((now, work))
        self._save_budget()  # Count even requests interrupted before a response arrives.
        return True

    def _get(self, url: str, work: Work) -> httpx.Response | None:
        if self.state.stopped or (
            self.state.retry_at is not None and self.clock() < self.state.retry_at
        ):
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
            except httpx.HTTPError:
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
                if retry > 86400:
                    self.state.stopped = "retry_after_exceeds_one_day"
                    self._save()
                    return
                delay = max(delay, retry)
            else:
                retry = 0
        else:
            retry = 0
        delay = min(86400, max(retry, delay * (0.8 + 0.4 * self.jitter())))
        self.state.retry_at = self.clock() + timedelta(seconds=delay)
        self._save()

    @staticmethod
    def _items(response: httpx.Response) -> list[dict[str, object]]:
        if response.status_code != 200:
            raise PollError("webex_request_failed", status=response.status_code)
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
        page_revisions: dict[str, int] = {}
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
            revision = page_revisions.get(day)
            if revision is None:
                # An open buffer is not an immutable Source yet: edits and late
                # messages before close update it in place (one page per Source).
                # Only a closed revision makes the next observation a correction.
                revision = space.revisions.get(day, 0)
                key = BundleKey(self.outbox.instance_id, sid, day, 0, revision)
                try:
                    closed = self.outbox.get(key.capture_id).state != "buffer"
                except ValueError as error:
                    if str(error) != "unknown_capture":
                        raise
                    closed = False
                revision += closed
            page_revisions[day] = revision
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
        if space.status != "ready" or (
            space.retry_at and self.clock() < space.retry_at
        ):
            return False
        scan = getattr(space, work)
        now = self.clock()
        if scan is None:
            finished = (
                space.incremental_at if work == "incremental" else space.overlap_at
            )
            if finished is not None and now < finished + timedelta(
                seconds=self.settings.poll_interval_seconds
                if work == "incremental"
                else self.settings.reconcile_interval_seconds
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
            self._save()  # Freeze the backfill/catch-up range before the request.
        url = self._safe_url(scan.url, "/v1/messages", sid)
        response = self._get(url, work)
        if response is None:
            return False
        if response.status_code in (403, 404):
            space.last_status = response.status_code
            space.gap_since = space.gap_since or now
            space.reason = (
                "forbidden" if response.status_code == 403 else "repeated_not_found"
            )
            space.not_found_count += 1
            if response.status_code == 403 or space.not_found_count >= 3:
                space.status = "access_gap"
            space.retry_at = now + timedelta(
                seconds=self.settings.poll_interval_seconds
            )
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
        self.accessible_spaces.add(sid)
        space.not_found_count = 0
        space.last_status = response.status_code
        space.retry_at = None
        space.reason = None
        space.gap_since = None
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
                space.synced_through = max(
                    space.synced_through or scan.ceiling, scan.ceiling
                )
            else:
                for mid, seen in space.seen.items():
                    if (
                        scan.floor <= seen.created < scan.ceiling
                        and mid not in scan.seen
                    ):
                        candidate = space.candidates.setdefault(
                            mid, Candidate(missing_at=now)
                        )
                        space.observations.setdefault(
                            mid, candidate.model_copy(deep=True)
                        )
                space.overlap_at = now
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
            if sid not in self.settings.exclude_space_ids and space.status == "ready"
        )
        if not ids:
            return
        attribute = f"turn_{work}"
        idle = 0
        while idle < len(ids):
            turn = getattr(self.state, attribute) % len(ids)
            setattr(self.state, attribute, turn + 1)
            sid = ids[turn]
            before = self.state.spaces[sid].model_copy(deep=True)
            try:
                advanced = self._page(sid, work)
            except PollError as error:
                if str(error) == "reauthentication_required":
                    raise
                # Initial scan bounds may have been frozen immediately before
                # this failed request. Restore the committed cursor, including
                # those bounds, rather than restarting backfill at a later date.
                saved = self.outbox.cursor(CURSOR)
                if saved:
                    before = State.model_validate_json(saved).spaces.get(sid, before)
                self.state.spaces[sid] = before
                before.status = "page_error"
                before.gap_since = before.gap_since or self.clock()
                before.reason = str(error)
                before.last_status = error.status or 200
                self._save()
                advanced = False
            if advanced:
                idle = 0
            else:
                idle += 1
        self._save()

    def _deletions(self) -> None:
        # A successful message list proves access only for that run. A missing
        # single message remains unverified (retention/revocation is ambiguous).
        candidates = sorted(
            (
                (candidate.checked_at or datetime.min.replace(tzinfo=UTC), sid, mid)
                for sid, space in self.state.spaces.items()
                if sid not in self.settings.exclude_space_ids
                and sid in self.accessible_spaces
                and space.status == "ready"
                for mid, candidate in space.candidates.items()
            ),
        )
        for _, sid, mid in candidates:
            space = self.state.spaces[sid]
            candidate = space.candidates[mid]
            if candidate.checked_at and self.clock() < candidate.checked_at + timedelta(
                seconds=self.settings.reconcile_interval_seconds
            ):
                continue
            response = self._get(f"{API}/messages/{quote(mid, safe='')}", "deletion")
            if response is None:
                return
            candidate.checked_at = self.clock()
            candidate.result = (
                "present" if response.status_code == 200 else "unverified"
            )
            space.observations[mid] = candidate.model_copy(deep=True)
            if response.status_code == 200:
                del space.candidates[mid]
            self._save()

    def _close_block(self, sid: str, day_end: datetime) -> str | None:
        """Fixed code for why a finished day cannot close yet (None: ready)."""
        space = self.state.spaces.get(sid)
        if space is None:
            return "unknown_space"
        if space.excluded or sid in self.settings.exclude_space_ids:
            return "excluded"
        if space.status != "ready":
            return f"space_{space.status}"
        if space.incremental is not None or space.overlap is not None:
            return "scan_in_progress"
        if space.synced_through is None or space.synced_through < day_end:
            return "not_synced"
        return None

    def _can_close(self, sid: str, day_end: datetime) -> bool:
        return self._close_block(sid, day_end) is None

    def _close_bundles(self) -> None:
        """Stage 7: freeze finished Space-days (and late corrections) for delivery."""
        known = {
            (sid, *_split_hold(slot))
            for sid, space in self.state.spaces.items()
            for slot in space.holds
        }
        result = close_ready(
            self.outbox,
            timezone=self.settings.timezone,
            now=self.clock(),
            eligible=self._can_close,
            max_payload_bytes=self.max_payload_bytes,
            known_holds=known,
        )
        holds: dict[str, dict[str, str]] = {}
        for (sid, day, revision), code in result.held.items():
            holds.setdefault(sid, {})[f"{day}#{revision}"] = code
        waiting: dict[str, dict[str, str]] = {}
        tz = ZoneInfo(self.settings.timezone)
        for sid, day in result.waiting:
            end = datetime.combine(
                date.fromisoformat(day) + timedelta(days=1), time.min, tzinfo=tz
            )
            waiting.setdefault(sid, {})[day] = self._close_block(sid, end) or "waiting"
        for sid, space in self.state.spaces.items():
            space.holds = holds.get(sid, {})
            space.close_waiting = waiting.get(sid, {})
        if result.capacity:
            self._save()
            raise CapacityError("outbox_capacity")

    def _discover_safely(self) -> None:
        if self.state.rooms_retry_at and self.clock() < self.state.rooms_retry_at:
            return
        try:
            self.discover(one_page=True)
            self.state.rooms_error = None
        except PollError as error:
            if str(error) == "reauthentication_required":
                raise
            self.state.rooms_error = str(error)
            self.state.rooms_retry_at = self.clock() + timedelta(
                seconds=self.settings.poll_interval_seconds
            )
            self._save()

    def _prune_seen(self) -> None:
        for space in self.state.spaces.values():
            floor = self.clock() - timedelta(days=self.settings.reconcile_overlap_days)
            for scan in (space.incremental, space.overlap):
                if scan:
                    floor = min(floor, scan.floor)
            space.seen = {
                mid: seen for mid, seen in space.seen.items() if seen.created >= floor
            }

    def tick(self) -> None:
        """Incremental has first claim; overlap is a separate resumable round."""
        # Discard in-memory mutations after any failed transaction/parse so that
        # the same Poller object is as safe to retry as a restarted process.
        try:
            self.accessible_spaces.clear()
            excluded = set(self.settings.exclude_space_ids)
            for sid in self.state.exclusions - excluded:
                if sid in self.state.spaces:
                    space = self.state.spaces[sid]
                    space.high_water = None
                    space.incremental = space.overlap = None
                    space.incremental_at = space.overlap_at = None
                    space.synced_through = None
                    space.status = "ready"
            self.state.exclusions = excluded
            # Stage 7 must check this durable marker before closing open buffers.
            for sid, space in self.state.spaces.items():
                space.excluded = sid in excluded
            self._prune_seen()
            self._save()
            if self.state.stopped:
                raise PollError("account_stopped")
            window = (
                int(self.clock().timestamp()) // self.settings.poll_interval_seconds
            )
            discovery_first = self.state.discovery_first
            if window != self.state.scheduled_window:
                self.state.scheduled_window = window
                self.state.discovery_first = not discovery_first
            if discovery_first or not self.state.spaces:
                self._discover_safely()
            self._round("incremental")
            self._discover_safely()
            self._round("incremental")
            self._round("overlap")
            self._deletions()
            self._prune_seen()
            self._close_bundles()
            self._save()
            if self.state.stopped:
                raise PollError("account_stopped")
        finally:
            raw = self.outbox.cursor(CURSOR)
            self.state = State.model_validate_json(raw) if raw else State()
            raw_budget = self.outbox.cursor(BUDGET)
            self.budget = (
                RequestBudget.model_validate_json(raw_budget)
                if raw_budget
                else RequestBudget()
            )
