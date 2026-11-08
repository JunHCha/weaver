"""Server-only Fabric projections; deliberately absent from SHARED_MODELS."""

from pydantic import Field, JsonValue

from .models import Contract, EntityId, Entry, EntryId, EntrySource


class StateValue(Contract):
    """A current value (including an explicit null) with its contributing fact."""

    value: JsonValue
    entry_id: EntryId
    source_id: EntrySource


class SetState(Contract):
    """Ordered members and the latest operation, including an empty/cleared set.

    Each member retains its last add evidence; entry_id/source_id identify the
    last operation, so removals and null clears remain attributable too.
    """

    members: list[StateValue]
    entry_id: EntryId
    source_id: EntrySource


class EntityState(Contract):
    """Replay of active facts, never action permission.

    Lists follow Fabric's UTC total order. Accumulated items and requests retain
    full Entry metadata. Decisions maps directly resolved request IDs to their
    latest active human decision, even if the request is no longer active.
    Original and inactive history remains available through Fabric.entries.
    """

    entity_id: EntityId
    fields: dict[str, StateValue] = Field(default_factory=dict)
    accumulated: list[Entry] = Field(default_factory=list)
    sets: dict[str, SetState] = Field(default_factory=dict)
    open_requests: list[Entry] = Field(default_factory=list)
    decisions: dict[str, Entry] = Field(default_factory=dict)
