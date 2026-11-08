"""ID-independent structured metrics and deterministic one-to-one matching."""

import json
from collections import Counter
from typing import Any

from weaver.loom.extract import normalize_name
from weaver.schema.internal import Extraction

GROUPS = ("entities", "entry_drafts", "schedules", "relations")
type Metric = dict[str, int | float | None]


def metric(numerator: int, denominator: int) -> Metric:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "score": numerator / denominator if denominator else None,
    }


def _key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def canonical(
    extraction: Extraction | None, ids: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    if extraction is None:
        return {group: [] for group in GROUPS}
    data = extraction.model_dump(mode="json", by_alias=True)
    known = {value: f"@{key}" for key, value in ids.items()}
    refs: dict[str, Any] = dict(known)
    for entity in data["entities"]:
        refs[entity["ref"]] = known.get(entity["entity_id"]) or [
            entity["type"],
            normalize_name(entity["name"]),
        ]

    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            return refs.get(value, value)
        if isinstance(value, list):
            return sorted((resolve(v) for v in value), key=_key)
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    def scored_row(item: dict[str, Any]) -> dict[str, Any]:
        row = {
            k: v
            for k, v in item.items()
            if k
            not in {
                "ref",
                "confidence",
                "reason",
                "thread_excerpt",
                "value",
                "title",
                "aliases",
                "ask",
            }
        }
        if "name" in row:
            row["name"] = normalize_name(row["name"])
        if "entity_id" in row:
            row["entity_id"] = known.get(row["entity_id"], row["entity_id"])
        for field in ("entities", "from", "to", "entry_ref", "blocked_by_request"):
            if field in row:
                row[field] = resolve(row[field])
        if row.get("request") is not None:
            row["request"] = {
                **row["request"],
                "requester": resolve(row["request"]["requester"]),
                "targets": resolve(row["request"]["targets"]),
            }
        ask = item["ask"]
        row["ask"] = (
            None
            if ask is None
            else sorted(
                known.get(c["entity_id"], c["entity_id"]) for c in ask["choices"]
            )
        )
        return row

    # Reference identities retain every scored field, not provider ref spelling.
    # Requests cannot themselves be blocked; resolve them before dependent drafts.
    for draft in sorted(data["entry_drafts"], key=lambda d: d["kind"] != "요청"):
        refs[draft["ref"]] = ["draft", scored_row(draft)]
    for schedule in data["schedules"]:
        refs[schedule["ref"]] = ["schedule", scored_row(schedule)]
    result = {
        group: sorted((scored_row(item) for item in data[group]), key=_key)
        for group in GROUPS
    }
    return result


def match(
    predicted: list[dict[str, Any]], gold: list[dict[str, Any]]
) -> list[tuple[int, int]]:
    """Hungarian assignment maximizes equal fields; sorted inputs break ties.

    Dummy rows represent omissions/extras. O(n^3), no factorial search.
    """
    n = max(len(predicted), len(gold))
    if not n:
        return []
    costs = [[0] * n for _ in range(n)]
    for i, p in enumerate(predicted):
        for j, g in enumerate(gold):
            costs[i][j] = -sum(p.get(k) == v for k, v in g.items())
    u, v, assignment, way = ([0] * (n + 1) for _ in range(4))
    for i in range(1, n + 1):
        assignment[0] = i
        j0 = 0
        minimum = [10**9] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = assignment[j0]
            delta, j1 = 10**9, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = costs[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minimum[j]:
                        minimum[j], way[j] = cur, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(n + 1):
                if used[j]:
                    u[assignment[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if assignment[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            assignment[j0] = assignment[j1]
            j0 = j1
    return sorted(
        (assignment[j] - 1, j - 1)
        for j in range(1, n + 1)
        if assignment[j] <= len(predicted) and j <= len(gold)
    )


def score_extraction(
    prediction: Extraction | None, alternatives: list[Extraction], ids: dict[str, str]
) -> dict[str, Any]:
    pred = canonical(prediction, ids)
    reports = []
    for alternative, gold_model in enumerate(alternatives or [None]):
        gold = canonical(gold_model, ids)
        metrics: dict[str, Metric] = {}
        matches = {}
        for group in GROUPS:
            p, g = pred[group], gold[group]
            pairs = match(p, g)
            matches[group] = [{"prediction": i, "gold": j} for i, j in pairs]
            tp = sum((Counter(map(_key, p)) & Counter(map(_key, g))).values())
            metrics[f"{group}.precision"] = metric(tp, len(p))
            metrics[f"{group}.recall"] = metric(tp, len(g))
            metrics[f"{group}.missing"] = metric(len(g) - tp, len(g))
            metrics[f"{group}.extra"] = metric(len(p) - tp, len(p))
            fields = sorted({k for row in [*p, *g] for k in row})
            for field in fields:
                metrics[f"{group}.field.{field}"] = metric(
                    sum(p[i].get(field) == g[j].get(field) for i, j in pairs),
                    max(len(p), len(g)),
                )
        for relation_type in ("소속", "참석", "원인", "후속조치", "같은논의"):
            pc = Counter(
                _key(r) for r in pred["relations"] if r["type"] == relation_type
            )
            gc = Counter(
                _key(r) for r in gold["relations"] if r["type"] == relation_type
            )
            tp = sum((pc & gc).values())
            metrics[f"relation.{relation_type}.precision"] = metric(tp, pc.total())
            metrics[f"relation.{relation_type}.recall"] = metric(tp, gc.total())
        metrics["exact"] = metric(int(pred == gold), 1)
        reports.append(
            {
                "alternative": alternative if alternatives else None,
                "metrics": metrics,
                "matches": matches,
                "prediction": pred,
                "gold": gold,
            }
        )
    # Select ONE complete alternative, never cherry-pick fields across answers.
    return max(
        reports,
        key=lambda r: (
            r["metrics"]["exact"]["numerator"],
            sum(
                m["numerator"] for name, m in r["metrics"].items() if ".field." in name
            ),
        ),
    )


def aggregate(rows: list[dict[str, Metric]]) -> dict[str, Metric]:
    names = sorted({name for row in rows for name in row})
    return {
        name: metric(
            sum(int(row.get(name, {}).get("numerator", 0) or 0) for row in rows),
            sum(int(row.get(name, {}).get("denominator", 0) or 0) for row in rows),
        )
        for name in names
    }
