"""Read private Wiki lineage without trusting editable frontmatter or prose.

Rules (ADR-046, "unknown is not sent"):

- A page with ``source_ids`` (page or section level) contributes those Sources.
- Otherwise its ``thread_ids`` are mapped back to their Fabric Source IDs.
- A page with neither is human-written: it has no ingested origin, so it
  contributes nothing. This is not an unknown.
- Lineage that exists but cannot be resolved (unreadable metadata, Thread IDs
  missing from Fabric) contributes the unknown placeholder. The policy allows
  that placeholder only while the archive holds no Webex Source.
"""

from collections.abc import Mapping

from weaver.llm.policy import UNKNOWN_WIKI_SOURCE

from .store import WikiStore

__all__ = ["UNKNOWN_WIKI_SOURCE", "source_ids"]


def _strings(value: object) -> set[str]:
    return (
        {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()
    )


def page_source_ids(
    metadata: Mapping[str, object], thread_sources: Mapping[str, str]
) -> set[str]:
    """Include page and section origins, resolving legacy Thread references."""
    known = _strings(metadata.get("source_ids"))
    threads = _strings(metadata.get("thread_ids"))
    sections = metadata.get("sections")
    if isinstance(sections, dict):
        for section in sections.values():
            if isinstance(section, dict):
                known |= _strings(section.get("source_ids"))
                threads |= _strings(section.get("thread_ids"))
    known |= {thread_sources[t] for t in threads if t in thread_sources}
    if threads - thread_sources.keys():
        known.add(UNKNOWN_WIKI_SOURCE)
    return known


def source_ids(store: WikiStore, thread_sources: Mapping[str, str]) -> set[str]:
    """``thread_sources`` maps Fabric Thread ID to its Source ID."""
    result: set[str] = set()
    try:
        pages = sorted((store.root / "pages").rglob("*.md"))
    except OSError:
        return {UNKNOWN_WIKI_SOURCE}
    for page in pages:
        try:
            metadata = store.metadata(page.relative_to(store.root).as_posix())
        except Exception:
            result.add(UNKNOWN_WIKI_SOURCE)
            continue
        known = _strings(metadata.get("source_ids"))
        threads = _strings(metadata.get("thread_ids"))
        sections = metadata.get("sections")
        if isinstance(sections, dict):
            for section in sections.values():
                if isinstance(section, dict):
                    known |= _strings(section.get("source_ids"))
                    threads |= _strings(section.get("thread_ids"))
        mapped = {thread_sources[t] for t in threads if t in thread_sources}
        if threads - thread_sources.keys():
            result.add(UNKNOWN_WIKI_SOURCE)
        result |= known | mapped
    return result
