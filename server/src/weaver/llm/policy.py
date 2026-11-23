"""ADR-046: the sole outbound policy/masking boundary, independent of backend.

Source IDs are private lineage, never inferred from prompt text or Receipt.
The resolver is supplied by the archive adapter; Webex integration supplies the
Space ID and the configured exclusions explicitly. Unknown inputs fail closed.
"""

import logging
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, get_args

from pydantic import BaseModel

from weaver.config import PrivacySettings
from weaver.schema.models import Channel

from .errors import LlmError
from .ledger import Ledger
from .types import LlmClient

logger = logging.getLogger(__name__)


class PolicyBlocked(LlmError):
    code = "policy_blocked"


class ProvenanceResolver(Protocol):
    def resolve(self, source_id: str) -> tuple[str, str | None] | None: ...


class Backlog(Protocol):
    def block(self, source_ids: frozenset[str]) -> None: ...

    def pending(self, source_ids: frozenset[str]) -> bool: ...


# Placeholders for lineage that cannot be named (shared dictionary prose, Wiki
# pages whose Threads are gone, human-only synthetic origins). They are never
# recorded in the backlog, including when supplied as the requester.
UNKNOWN_DICTIONARY_SOURCE = "unknown_dictionary_provenance"
UNKNOWN_WIKI_SOURCE = "unknown_wiki_provenance"
UNKNOWN_LINEAGE = frozenset({UNKNOWN_DICTIONARY_SOURCE, UNKNOWN_WIKI_SOURCE})
# Proven origin: the user typed this dictionary content in the web editor. It is
# not unknown lineage and never stands in for a Source; Webex Source IDs mixed
# into the same entry are still judged by their Space.
HUMAN_DICTIONARY_SOURCE = "human_dictionary_edit"

# Every channel except Webex is allowed by default; Webex needs its Space ID.
ALLOWED_CHANNELS: frozenset[str] = frozenset(get_args(Channel.__value__)) - {"webex"}


@dataclass(frozen=True)
class Provenance:
    source_ids: frozenset[str]
    shared: bool = False
    # The one Source whose work is waiting on this call, if there is one.
    requester: str | None = None


_current: ContextVar[Provenance | None] = ContextVar("llm_provenance", default=None)


@contextmanager
def provenance_scope(
    source_ids: Iterable[str],
    *,
    shared: bool = False,
    requester: str | None = None,
) -> Iterator[None]:
    parent = _current.get()
    value = Provenance(
        frozenset(source_ids) | (parent.source_ids if parent else frozenset()),
        shared or bool(parent and parent.shared),
        requester or (parent.requester if parent else None),
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


_MASK = "[MASKED]"
_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----"
        r".*?-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----",
        re.DOTALL,
    ),
    re.compile(
        r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|sk-[A-Za-z0-9_-]{20,}"
    ),
)
_DECORATION = r"[*_|`\"' \t]*"
_SEPARATOR = r"(?:[:=：]|는|은|\|)"
_PASSWORD_NAMES = r"password|passwd|pw|pass|비밀번호|패스워드|암호"
_OTHER_NAMES = r"secret|token|api[_-]?key|private[_-]?key"
# Phrases and "id/pw" pairs run to the end of the line (or table cell).
_PASSWORD_ASSIGNMENT = re.compile(
    rf"(?i)((?<![A-Za-z])(?:{_PASSWORD_NAMES}){_DECORATION}{_SEPARATOR}{_DECORATION})"
    r"[^\n|]*[^\s|]"
)
_OTHER_ASSIGNMENT = re.compile(
    rf"(?i)((?<![A-Za-z])(?:{_OTHER_NAMES}){_DECORATION}{_SEPARATOR}{_DECORATION})"
    r"(?:(?:Bearer|Basic)\s+)?[^\s|]+"
)
# Long base64-like runs; "-" and "_" are excluded so slugs never match.
_LONG_TOKEN = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=]{32,}(?![A-Za-z0-9+/=_-])"
)
# Unlabeled base64url secrets: must mix digits, lower and upper case so that
# lowercase slugs stay untouched.
_URLSAFE_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
_WIKI_LINK = re.compile(r"(\[\[[^\]\n]*\]\])")
# Short keys written after a "키"/"key"/"pw" label on the same line.
_KEY_LABEL = re.compile(r"(?i)(?<![가-힣A-Za-z])(?:키|key|pw)(?![가-힣A-Za-z])")
_SHORT_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,31}(?![A-Za-z0-9_-])")


def _mixed_token(match: re.Match[str]) -> str:
    value = match.group()
    if not (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)):
        return value
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value  # git SHA-1
    if "/" in value and not (
        "+" in value
        or "=" in value
        or (any(c.islower() for c in value) and any(c.isupper() for c in value))
    ):
        return value  # path-like
    return _MASK


def _urlsafe_token(match: re.Match[str]) -> str:
    value = match.group()
    if (
        any(c.isdigit() for c in value)
        and any(c.islower() for c in value)
        and any(c.isupper() for c in value)
    ):
        return _MASK
    return value


def _labeled_short_keys(line: str) -> str:
    label = _KEY_LABEL.search(line)
    if label is None:
        return line
    head, tail = line[: label.end()], line[label.end() :]
    return head + _SHORT_TOKEN.sub(_urlsafe_token, tail)


def _mask_plain(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_MASK, text)
    text = _PASSWORD_ASSIGNMENT.sub(lambda m: m.group(1) + _MASK, text)
    text = _OTHER_ASSIGNMENT.sub(lambda m: m.group(1) + _MASK, text)
    text = _LONG_TOKEN.sub(_mixed_token, text)
    text = _URLSAFE_TOKEN.sub(_urlsafe_token, text)
    return "\n".join(_labeled_short_keys(line) for line in text.split("\n"))


def mask_secrets(text: str) -> str:
    """Hide credentials in text kept for reference search (ADR-046).

    Wiki links ``[[...]]`` are never touched.
    """
    parts = _WIKI_LINK.split(text)
    return "".join(
        part if index % 2 else _mask_plain(part) for index, part in enumerate(parts)
    )


class LlmPolicy:
    def __init__(
        self,
        resolver: ProvenanceResolver | None = None,
        *,
        settings: PrivacySettings | None = None,
        excluded_space_ids: frozenset[str] | None = None,
        shared_sources: Callable[[], Iterable[str]] | None = None,
        backlog: Backlog | None = None,
        webex_present: Callable[[], bool] | None = None,
        normalize_sources: Callable[[frozenset[str]], dict[str, str]] | None = None,
    ) -> None:
        self.resolver = resolver
        self.settings = settings if settings is not None else PrivacySettings()
        # None means the exclusions have not been wired, not an empty allowlist.
        self.excluded_space_ids = excluded_space_ids
        self.shared_sources = shared_sources
        self.backlog = backlog
        # Reports whether the archive holds any Webex Source. Without it,
        # unknown lineage always fails closed.
        self.webex_present = webex_present
        self.normalize_sources = normalize_sources

    def prepare(self, system: str, user: str) -> PreparedInput:
        provenance = _current.get()
        if provenance is None:
            raise PolicyBlocked()
        ids = provenance.source_ids
        shared = provenance.shared
        if shared:
            if self.shared_sources is None:
                raise PolicyBlocked()  # not wired: nothing to record
            try:
                ids |= frozenset(self.shared_sources())
            except Exception:
                raise PolicyBlocked() from None  # transient: evaluate again later
        provenance = Provenance(ids, requester=provenance.requester)
        self._evaluate(provenance, allow_empty=shared)
        # Never mask a denied input; masking cannot grant permission.
        return PreparedInput(mask_patterns(system), mask_patterns(user), provenance)

    def check(self, provenance: Provenance) -> None:
        self._evaluate(provenance, allow_empty=False)

    def _closed(self, ids: frozenset[str]) -> set[str]:
        """IDs that are closed or unknown now; a transient fault blocks this call."""
        closed: set[str] = set()
        for source_id in sorted(ids):
            if source_id == HUMAN_DICTIONARY_SOURCE:
                continue  # user-entered content, allowed even after Webex ingest
            if source_id == UNKNOWN_DICTIONARY_SOURCE:
                # The dictionary's persisted write-time flag already established
                # uncertainty. Removing a Source/Receipt cannot erase that fact.
                closed.add(source_id)
                continue
            if source_id in UNKNOWN_LINEAGE:
                # Unknown lineage is proven non-Webex only while the archive
                # holds no Webex Source at all (ADR-046: unknown is not sent).
                try:
                    proven = self.webex_present is not None and not self.webex_present()
                except Exception:
                    raise PolicyBlocked() from None
                if not proven:
                    closed.add(source_id)
                continue
            if self.resolver is None:
                closed.add(source_id)
                continue
            try:
                origin = self.resolver.resolve(source_id)
            except Exception:
                raise PolicyBlocked() from None
            if origin is None:
                closed.add(source_id)
                continue
            channel, space_id = origin
            if channel == "webex":
                if (
                    not self.settings.channels.webex.llm_allowed
                    or not space_id
                    or self.excluded_space_ids is None
                    or space_id in self.excluded_space_ids
                ):
                    closed.add(source_id)
            elif channel not in ALLOWED_CHANNELS:
                closed.add(source_id)
        return closed

    def _evaluate(self, provenance: Provenance, *, allow_empty: bool) -> None:
        ids = provenance.source_ids
        requester = provenance.requester
        if self.normalize_sources is not None:
            try:
                # One read of the origin sets per evaluation, including requester.
                aliases = self.normalize_sources(
                    ids | ({requester} if requester else set())
                )
                ids = frozenset(aliases.get(sid, sid) for sid in ids)
                if requester is not None:
                    requester = aliases.get(requester, requester)
            except Exception:
                raise PolicyBlocked() from None
        if not ids:
            if allow_empty:
                return  # shared context was read completely and has no lineage
            raise PolicyBlocked()
        closed = self._closed(ids)
        if closed:
            # Block this call, and remember only what can actually be resumed:
            # the closed/unknown origins and the one Source waiting on them.
            held = (closed | ({requester} if requester else set())) - UNKNOWN_LINEAGE
            self._remember(held)
            raise PolicyBlocked()
        if self.backlog is not None:
            watched = ids | ({requester} if requester else set())
            try:
                pending = self.backlog.pending(watched)
            except Exception:
                raise PolicyBlocked() from None
            if pending:
                raise PolicyBlocked()

    def _remember(self, held: set[str]) -> None:
        if self.backlog is None or not held:
            return
        try:
            self.backlog.block(frozenset(held))
        except Exception:
            logger.warning("policy backlog write failed")


def default_policy() -> LlmPolicy:
    """Direct API/CLI constructors also fail closed until explicitly configured."""
    return LlmPolicy()


class ScopedClient:
    """Carry original Source IDs through rewrites, summaries and extraction."""

    def __init__(
        self,
        client: LlmClient,
        source_ids: Iterable[str],
        *,
        shared: bool = False,
        requester: str | None = None,
    ) -> None:
        self.client = client
        self.source_ids = tuple(source_ids)
        self.shared = shared
        self.requester = requester

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        with provenance_scope(
            self.source_ids, shared=self.shared, requester=self.requester
        ):
            return self.client.generate_json(task, system, user, schema)

    def generate_text(self, task: str, system: str, user: str) -> str:
        with provenance_scope(
            self.source_ids, shared=self.shared, requester=self.requester
        ):
            return self.client.generate_text(task, system, user)


class PolicyClient:
    """Enforce the gate on any client, including an injected one."""

    def __init__(self, client: LlmClient, policy: LlmPolicy) -> None:
        self.client = client
        self.policy = policy

    def generate_json[T: BaseModel](
        self, task: str, system: str, user: str, schema: type[T]
    ) -> T:
        prepared = self.policy.prepare(system, user)
        return self.client.generate_json(task, prepared.system, prepared.user, schema)

    def generate_text(self, task: str, system: str, user: str) -> str:
        prepared = self.policy.prepare(system, user)
        return self.client.generate_text(task, prepared.system, prepared.user)


class LedgerBacklog:
    """Policy holds share the private accounting DB, including preview snapshots."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def block(self, source_ids: frozenset[str]) -> None:
        self.ledger.policy_block(source_ids)

    def pending(self, source_ids: frozenset[str]) -> bool:
        return any(sid in source_ids for sid, _ in self.ledger.policy_backlog())
