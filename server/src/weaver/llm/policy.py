"""ADR-046: the sole outbound policy/masking boundary, independent of backend.

Source IDs are private lineage, never inferred from prompt text or Receipt.
The resolver is supplied by the archive adapter; Webex integration supplies the
Space ID and the configured exclusions explicitly. Unknown inputs fail closed.
"""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import NoReturn, Protocol

from pydantic import BaseModel

from weaver.config import PrivacySettings

from .errors import LlmError
from .ledger import Ledger
from .types import LlmClient


class PolicyBlocked(LlmError):
    code = "policy_blocked"


class ProvenanceResolver(Protocol):
    def resolve(self, source_id: str) -> tuple[str, str | None] | None: ...


class Backlog(Protocol):
    def block(self, source_ids: frozenset[str]) -> None: ...

    def pending(self, source_ids: frozenset[str]) -> bool: ...


@dataclass(frozen=True)
class Provenance:
    source_ids: frozenset[str]
    shared: bool = False


_current: ContextVar[Provenance | None] = ContextVar("llm_provenance", default=None)


@contextmanager
def provenance_scope(
    source_ids: Iterable[str], *, shared: bool = False
) -> Iterator[None]:
    parent = _current.get()
    value = Provenance(
        frozenset(source_ids) | (parent.source_ids if parent else frozenset()),
        shared or bool(parent and parent.shared),
    )
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


@dataclass(frozen=True)
class PreparedInput:
    system: str
    user: str
    provenance: Provenance


def mask_patterns(text: str) -> str:
    """The single masking hook. ADR-046 keeps MVP masking OFF and names intact."""
    return text


class LlmPolicy:
    def __init__(
        self,
        resolver: ProvenanceResolver | None = None,
        *,
        settings: PrivacySettings | None = None,
        excluded_space_ids: frozenset[str] | None = None,
        shared_sources: Callable[[], Iterable[str]] | None = None,
        backlog: Backlog | None = None,
    ) -> None:
        self.resolver = resolver
        self.settings = settings if settings is not None else PrivacySettings()
        # None means the exclusions have not been wired, not an empty allowlist.
        self.excluded_space_ids = excluded_space_ids
        self.shared_sources = shared_sources
        self.backlog = backlog

    def prepare(self, system: str, user: str) -> PreparedInput:
        provenance = _current.get()
        if provenance is None:
            raise PolicyBlocked()
        ids = provenance.source_ids
        if provenance.shared:
            if self.shared_sources is None:
                self._deny(ids)
            else:
                try:
                    ids |= frozenset(self.shared_sources())
                except Exception:
                    self._deny(ids)
        provenance = Provenance(ids)
        self.check(provenance)
        # Never mask a denied input; masking cannot grant permission.
        return PreparedInput(mask_patterns(system), mask_patterns(user), provenance)

    def _deny(self, ids: frozenset[str]) -> NoReturn:
        if self.backlog is not None and ids:
            self.backlog.block(ids)
        raise PolicyBlocked()

    def check(self, provenance: Provenance) -> None:
        ids = provenance.source_ids
        if self.resolver is None or not ids:
            self._deny(ids)
        assert self.resolver is not None
        for source_id in sorted(ids):
            try:
                origin = self.resolver.resolve(source_id)
            except Exception:
                self._deny(ids)
            if origin is None:
                self._deny(ids)
            assert origin is not None
            channel, space_id = origin
            if channel == "webex":
                if (
                    not self.settings.channels.webex.llm_allowed
                    or not space_id
                    or self.excluded_space_ids is None
                    or space_id in self.excluded_space_ids
                ):
                    self._deny(ids)
            elif channel not in {"call", "voice_memo", "kakao", "share_text"}:
                self._deny(ids)
        if self.backlog is not None and self.backlog.pending(ids):
            raise PolicyBlocked()


def default_policy() -> LlmPolicy:
    """Direct API/CLI constructors also fail closed until explicitly configured."""
    return LlmPolicy()


class ScopedClient:
    """Carry original Source IDs through rewrites, summaries and extraction."""

    def __init__(
        self, client: LlmClient, source_ids: Iterable[str], *, shared: bool = False
    ) -> None:
        self.client = client
        self.source_ids = tuple(source_ids)
        self.shared = shared

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        with provenance_scope(self.source_ids, shared=self.shared):
            return self.client.generate_json(task, system, user, schema)

    def generate_text(self, task: str, system: str, user: str) -> str:
        with provenance_scope(self.source_ids, shared=self.shared):
            return self.client.generate_text(task, system, user)


class LedgerBacklog:
    """Policy holds share the private accounting DB, including preview snapshots."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def block(self, source_ids: frozenset[str]) -> None:
        self.ledger.policy_block(source_ids)

    def pending(self, source_ids: frozenset[str]) -> bool:
        return any(sid in source_ids for sid, _ in self.ledger.policy_backlog())
