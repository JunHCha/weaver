"""Complete older synthetic provider fixtures with the shared broad area contract.

Individual area validation tests intentionally bypass this fixture builder.
"""

from copy import deepcopy

from pydantic import BaseModel


def fixture_response[T: BaseModel](schema: type[T], data: object) -> T:
    from weaver.loom.summary_review import ApprovedResponse
    from weaver.wiki.compiler import EditResult

    if schema not in (ApprovedResponse, EditResult) or not isinstance(data, dict):
        return schema.model_validate(data)
    payload = deepcopy(data)
    payload = {
        key: [
            item.model_dump() if isinstance(item, BaseModel) else item for item in value
        ]
        if isinstance(value, list)
        else value
        for key, value in payload.items()
    }
    rows = payload.get("threads", payload.get("changes", []))
    business = [row for row in rows if not row.get("correction")]
    if business:
        additions = payload.setdefault("new_entries", [])
        if not any(entry["name"] == "업무 운영" for entry in additions):
            additions.append({"name": "업무 운영", "kind": "area", "aliases": []})
        for row in business:
            if "업무 운영" not in row["tags"]:
                row["tags"].append("업무 운영")
    return schema.model_validate(payload)


def edit_response(schema, user, data):
    """Upgrade old synthetic edit fixtures to the per-hunk verdict contract."""
    import json

    result = fixture_response(schema, data)
    hunks = json.loads(user).get("hunks", [])
    changes = result.changes
    return schema.model_validate(
        result.model_dump()
        | {
            "changes": [],
            "hunks": [
                {
                    "id": h["id"],
                    "classification": "fact" if changes and i == 0 else "expression",
                    "changes": [c.model_dump() for c in changes] if i == 0 else [],
                }
                for i, h in enumerate(hunks)
            ],
        }
    )
