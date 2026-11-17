import type { Tag, Thread } from "./api";
import { fieldLabel, fieldText, objectValue, relations } from "./presentation";

export function isHistory(t: Thread): boolean {
  // Compatibility with older snapshots; keep in sync with web.presentation() in
  // server/src/weaver/web/__init__.py. The API's explicit presentation wins.
  return (
    t.presentation === "history" ||
    (t.presentation === undefined &&
      (["실행", "태그정정", "거절"].includes(t.kind) ||
        !!t.retracts ||
        t.thread_id.startsWith("wiki-field:") ||
        (t.thread_id.startsWith("wiki:") &&
          !!t.field &&
          t.content === `${t.field} 변경`)))
  );
}
export function historyTarget(t: Thread): string | null {
  return (
    t.correction?.target || t.retracts || t.executes || t.resolves || t.updates
  );
}
export function isValueHistory(t: Thread): boolean {
  return isHistory(t) && !!t.field && !historyTarget(t);
}
export function stateTag(t: Thread, tags: Tag[]): string | undefined {
  const kinds = new Map(tags.map((tag) => [tag.name, tag.kind]));
  return (
    t.tags.find((name) => kinds.get(name) === "project") ||
    t.tags.find((name) => kinds.get(name) === "area")
  );
}
export function mentionOnly(
  t: Thread,
  selected: string[],
  tags: Tag[],
): boolean {
  return mentionMatch(
    t,
    selected,
    new Set(
      tags
        .filter((tag) => tag.kind === "person" || tag.kind === "organization")
        .map((tag) => tag.name),
    ),
  );
}
export function mentionMatch(
  t: Thread,
  selected: string[],
  peopleTags: ReadonlySet<string>,
): boolean {
  const matches = selected.filter(
    (name) => t.tags.includes(name) || t.counterpart === name,
  );
  return (
    matches.length > 0 &&
    matches.every((name) => peopleTags.has(name) && t.counterpart !== name)
  );
}
export interface ValueChange {
  thread: Thread;
  label: string;
  target: string;
}
export interface ThreadView {
  byId: Map<string, Thread>;
  relation: ReturnType<typeof relations>;
  knownTags: Set<string>;
  peopleTags: Set<string>;
  peopleById: Map<string, Set<string>>;
  personThreads: Map<string, Set<string>>;
  children: Map<string, Thread[]>;
  targets: Map<string, Thread>;
  changes: ValueChange[];
  fields: Map<string, [string, string] | null>;
}
function push<K, V>(map: Map<K, V[]>, key: K, value: V) {
  const list = map.get(key);
  if (list) list.push(value);
  else map.set(key, [value]);
}
export function historyIndex(rows: Thread[]): Map<string, Thread[]> {
  const children = new Map<string, Thread[]>();
  for (const t of rows) {
    const target = isHistory(t) && historyTarget(t);
    if (target) push(children, target, t);
  }
  return children;
}
/** Iterative traversal is bounded by the snapshot IDs, even with cyclic refs. */
export function threadHistory(
  target: Thread,
  snapshot: Thread[],
  children = historyIndex(snapshot),
): Thread[] {
  const seen = new Set([target.thread_id]),
    queue = [target.thread_id],
    result: Thread[] = [];
  for (let i = 0; i < queue.length; i++) {
    for (const t of children.get(queue[i]) || []) {
      if (seen.has(t.thread_id)) continue;
      seen.add(t.thread_id);
      queue.push(t.thread_id);
      result.push(t);
    }
  }
  return result.sort(
    (a, b) =>
      Date.parse(a.recorded_at) - Date.parse(b.recorded_at) || a.seq - b.seq,
  );
}
/** One visit per row/edge; no recursive stack or repeated full-list scans. */
export function adjacentHistory(rows: Thread[]): Thread[] {
  const pending = new Set(rows.map((t) => t.thread_id)),
    children = historyIndex(rows),
    result: Thread[] = [];
  const roots = rows.filter(
    (t) => !isHistory(t) || !pending.has(historyTarget(t) || ""),
  );
  for (const root of [...roots, ...rows]) {
    if (!pending.has(root.thread_id)) continue;
    const stack = [root];
    while (stack.length) {
      const t = stack.pop()!;
      if (!pending.delete(t.thread_id)) continue;
      result.push(t);
      const next = children.get(t.thread_id) || [];
      for (let i = next.length - 1; i >= 0; i--)
        if (pending.has(next[i].thread_id)) stack.push(next[i]);
    }
  }
  return result;
}
function ownField(t: Thread): [string, string] | null {
  const field = fieldText(t);
  if (field) return field;
  if (t.field && t.value === null) return [fieldLabel[t.field], "없음"];
  if (!isHistory(t)) return null;
  if (t.correction)
    return [
      "태그",
      [
        ...t.correction.add.map((n) => `+${n}`),
        ...t.correction.remove.map((n) => `−${n}`),
      ].join(", ") || "변경 없음",
    ];
  if (t.retracts) return ["철회", t.retracts];
  if (t.value !== null) {
    const value = objectValue(t.value);
    return [
      t.executes ? "실행 결과" : "값",
      typeof value?.outcome === "string"
        ? value.outcome
        : typeof t.value === "string"
          ? t.value
          : JSON.stringify(t.value),
    ];
  }
  return null;
}
export function createThreadView(snapshot: Thread[], tags: Tag[]): ThreadView {
  const byId = new Map(snapshot.map((t) => [t.thread_id, t])),
    relation = relations(snapshot);
  const kinds = new Map(tags.map((tag) => [tag.name, tag.kind]));
  const knownTags = new Set(
    tags.filter((tag) => tag.kind).map((tag) => tag.name),
  );
  const peopleTags = new Set(
    tags
      .filter((tag) => tag.kind === "person" || tag.kind === "organization")
      .map((tag) => tag.name),
  );
  const peopleById = new Map<string, Set<string>>(),
    personThreads = new Map<string, Set<string>>(),
    fields = new Map<string, [string, string] | null>();
  const targets = new Map<string, Thread>(),
    changes: ValueChange[] = [];
  const states = new Map<string, Map<string, Thread>>(),
    assignees = new Map<string, Set<string>>();
  // API snapshots are ordered by occurrence; sort a copy for legacy/test callers.
  const ordered = [...snapshot].sort(
    (a, b) =>
      Date.parse(a.occurred_at) - Date.parse(b.occurred_at) || a.seq - b.seq,
  );
  for (const t of ordered) {
    fields.set(t.thread_id, ownField(t));
    const names = new Set(t.tags.filter((name) => peopleTags.has(name)));
    if (t.counterpart) names.add(t.counterpart);
    peopleById.set(t.thread_id, names);
    for (const name of names) {
      const ids = personThreads.get(name) || new Set<string>();
      ids.add(t.thread_id);
      personThreads.set(name, ids);
    }
    if (!t.field || t.retracts || ["실행", "태그정정", "거절"].includes(t.kind))
      continue;
    const targetTags = t.tags.filter(
      (name) => kinds.get(name) === "project" || kinds.get(name) === "area",
    );
    const primary =
      targetTags.find((name) => kinds.get(name) === "project") || targetTags[0];
    if (isValueHistory(t) && primary) {
      const before = states.get(primary)?.get(t.field);
      const oldText = before ? fieldText(before)?.[1] : null;
      let after = t;
      if (t.field === "assignees") {
        const next = new Set(assignees.get(primary));
        applyAssignees(next, t);
        after = { ...t, value: { add: [...next], remove: [] } };
      }
      changes.push({
        thread: t,
        target: primary,
        label: `${fieldLabel[t.field]} ${oldText || "없음"} → ${fieldText(after)?.[1] || "없음"}${relation.retracted.has(t.thread_id) ? " · 철회됨" : ""}`,
      });
    }
    if (relation.retracted.has(t.thread_id)) continue;
    for (const name of targetTags) {
      const state = states.get(name) || new Map<string, Thread>();
      if (t.field === "assignees") {
        const names = assignees.get(name) || new Set<string>();
        applyAssignees(names, t);
        assignees.set(name, names);
        state.set(t.field, { ...t, value: { add: [...names], remove: [] } });
      } else state.set(t.field, t);
      states.set(name, state);
    }
  }
  // Resolve each history chain once, caching every member on the traversed path.
  for (const row of snapshot) {
    if (targets.has(row.thread_id)) continue;
    const path: Thread[] = [],
      seen = new Set<string>();
    let t = row;
    while (!targets.has(t.thread_id) && !seen.has(t.thread_id)) {
      path.push(t);
      seen.add(t.thread_id);
      const id = isHistory(t) ? historyTarget(t) : null;
      const next = id ? byId.get(id) : undefined;
      if (!next) break;
      t = next;
    }
    const root = targets.get(t.thread_id) || t;
    path.forEach((item) => targets.set(item.thread_id, root));
  }
  return {
    byId,
    relation,
    knownTags,
    peopleTags,
    peopleById,
    personThreads,
    children: historyIndex(snapshot),
    targets,
    changes,
    fields,
  };
}
function applyAssignees(names: Set<string>, t: Thread) {
  if (t.value === null) {
    names.clear();
    return;
  }
  const value = objectValue(t.value);
  if (Array.isArray(value?.add))
    value.add.forEach((n) => {
      if (typeof n === "string") names.add(n);
    });
  if (Array.isArray(value?.remove))
    value.remove.forEach((n) => {
      if (typeof n === "string") names.delete(n);
    });
}
// Convenience for focused unit tests; the UI builds this once per snapshot.
export function valueChanges(
  rows: Thread[],
  snapshot: Thread[],
  tags: Tag[],
): ValueChange[] {
  const ids = new Set(rows.map((t) => t.thread_id));
  return createThreadView(snapshot, tags).changes.filter(({ thread }) =>
    ids.has(thread.thread_id),
  );
}
export function distribute<T>(
  items: T[],
  keys: (item: T) => string[],
): Map<string, T[]> {
  const result = new Map<string, T[]>();
  for (const item of items)
    for (const key of keys(item)) push(result, key, item);
  return result;
}
