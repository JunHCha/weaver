import type { Tag, Thread } from "./api";
import { fieldLabel, fieldText, relations } from "./presentation";
import { projectRows } from "./projection";

export function isHistory(t: Thread): boolean {
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
export function people(t: Thread, tags: Tag[]): Set<string> {
  return new Set([
    ...(t.counterpart ? [t.counterpart] : []),
    ...t.tags.filter((name) =>
      tags.some((tag) => tag.name === name && tag.kind === "person"),
    ),
  ]);
}
export function mentionOnly(
  t: Thread,
  selected: string[],
  tags: Tag[],
): boolean {
  const names = selected.filter((name) =>
    tags.some((tag) => tag.name === name && tag.kind === "person"),
  );
  return (
    names.length > 0 &&
    !names.includes(t.counterpart || "") &&
    names.some((name) => t.tags.includes(name))
  );
}
export function stateTag(t: Thread, tags: Tag[]): string | undefined {
  return (
    ["project", "area"].flatMap((kind) =>
      t.tags.filter((name) =>
        tags.some((tag) => tag.name === name && tag.kind === kind),
      ),
    )[0] || t.tags[0]
  );
}
/** Resolve field chips from the full snapshot, never from the filtered rows. */
export function currentThread(
  t: Thread,
  snapshot: Thread[],
  tags: Tag[],
): Thread {
  if (!t.field || isHistory(t)) return t;
  const name = stateTag(t, tags);
  if (!name) return t;
  const state = projectRows(
    snapshot.filter((row) => row.tags.includes(name)),
    snapshot,
  );
  return {
    ...t,
    value:
      t.field === "assignees"
        ? { add: state.assignees, remove: [] }
        : state[t.field],
  };
}
export function valueChanges(
  rows: Thread[],
  snapshot: Thread[],
  tags: Tag[],
): { thread: Thread; label: string }[] {
  const active = relations(snapshot);
  return rows.filter(isValueHistory).map((t) => {
    const name = stateTag(t, tags);
    const before = snapshot.filter(
      (row) =>
        (Date.parse(row.occurred_at) < Date.parse(t.occurred_at) ||
          (row.occurred_at === t.occurred_at && row.seq < t.seq)) &&
        !row.retracts &&
        !active.retracted.has(row.thread_id) &&
        !!name &&
        row.tags.includes(name),
    );
    const previous = projectRows(before, snapshot);
    const prior = {
      ...t,
      value:
        t.field === "assignees"
          ? { add: previous.assignees, remove: [] }
          : previous[t.field!],
    };
    const next = projectRows(
      [...before, t],
      [...snapshot.filter((row) => row.retracts !== t.thread_id), t],
    );
    const after = {
      ...t,
      value:
        t.field === "assignees"
          ? { add: next.assignees, remove: [] }
          : next[t.field!],
    };
    return {
      thread: t,
      label: `${fieldLabel[t.field!]} ${fieldText(prior)?.[1] || "없음"} → ${fieldText(after)?.[1] || "없음"}${active.retracted.has(t.thread_id) ? " · 철회됨" : ""}`,
    };
  });
}

export function threadHistory(target: Thread, snapshot: Thread[]): Thread[] {
  const ids = new Set([target.thread_id]);
  // Retractions of corrections/executions belong to the same target history.
  for (let i = 0; i < snapshot.length; i++) {
    let changed = false;
    snapshot.forEach((row) => {
      if (
        isHistory(row) &&
        ids.has(historyTarget(row) || "") &&
        !ids.has(row.thread_id)
      ) {
        ids.add(row.thread_id);
        changed = true;
      }
    });
    if (!changed) break;
  }
  return snapshot
    .filter(
      (row) => row.thread_id !== target.thread_id && ids.has(row.thread_id),
    )
    .sort(
      (a, b) =>
        Date.parse(a.recorded_at) - Date.parse(b.recorded_at) || a.seq - b.seq,
    );
}

export function adjacentHistory(rows: Thread[]): Thread[] {
  const pending = new Set(rows.map((row) => row.thread_id));
  const result: Thread[] = [];
  const append = (row: Thread) => {
    if (!pending.delete(row.thread_id)) return;
    result.push(row);
    rows
      .filter(
        (next) => isHistory(next) && historyTarget(next) === row.thread_id,
      )
      .forEach(append);
  };
  rows
    .filter(
      (row) =>
        !isHistory(row) ||
        !rows.some((target) => target.thread_id === historyTarget(row)),
    )
    .forEach(append);
  rows.forEach(append);
  return result;
}
