import type { Thread } from "./api";
import type { TagState } from "./fabric";
import { objectValue, relations } from "./presentation";
export function projectRows(rows: Thread[], snapshot: Thread[]): TagState {
  const relation = relations(snapshot),
    assignees = new Set<string>();
  const state: TagState = {
    deadline: null,
    schedule: null,
    status: null,
    assignees: [],
    open_requests: [],
  };
  rows.forEach((t) => {
    if (
      relation.retracted.has(t.thread_id) ||
      t.retracts ||
      ["태그정정", "거절", "실행"].includes(t.kind)
    )
      return;
    if (relation.isOpen(t)) state.open_requests.push(t.thread_id);
    const value = objectValue(t.value);
    if (t.field === "assignees") {
      if (t.value === null) assignees.clear();
      else if (value) {
        if (Array.isArray(value.add))
          value.add.forEach((n) => {
            if (typeof n === "string") assignees.add(n);
          });
        if (Array.isArray(value.remove))
          value.remove.forEach((n) => {
            if (typeof n === "string") assignees.delete(n);
          });
      }
    } else if (t.field === "deadline")
      state.deadline = typeof t.value === "string" ? t.value : null;
    else if (t.field === "status")
      state.status =
        t.value === null
          ? null
          : typeof t.value === "string"
            ? t.value
            : JSON.stringify(t.value);
    else if (t.field === "schedule")
      state.schedule =
        value &&
        typeof value.start === "string" &&
        typeof value.timezone === "string"
          ? {
              start: value.start,
              end: typeof value.end === "string" ? value.end : null,
              timezone: value.timezone,
            }
          : null;
  });
  state.assignees = [...assignees];
  return state;
}
