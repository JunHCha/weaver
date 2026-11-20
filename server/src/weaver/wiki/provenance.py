"""Read private Wiki lineage without trusting editable frontmatter or prose."""

from .store import WikiStore

UNKNOWN_WIKI_SOURCE = "unknown_wiki_provenance"


def source_ids(store: WikiStore) -> set[str]:
    result: set[str] = set()
    for page in (store.root / "pages").glob("*.md"):
        try:
            metadata = store.metadata(page.relative_to(store.root).as_posix())
        except Exception:
            result.add(UNKNOWN_WIKI_SOURCE)
            continue
        ids = metadata.get("source_ids", [])
        known = (
            {sid for sid in ids if isinstance(sid, str)}
            if isinstance(ids, list)
            else set()
        )
        mapping = metadata.get("sections", {})
        if isinstance(mapping, dict):
            for section in mapping.values():
                if isinstance(section, dict):
                    values = section.get("source_ids", [])
                    if isinstance(values, list):
                        known.update(sid for sid in values if isinstance(sid, str))
        result.update(known or {UNKNOWN_WIKI_SOURCE})
    return result
