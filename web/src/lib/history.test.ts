import { describe, expect, it } from "vitest";
import type { Tag, Thread } from "./api";
import {
  adjacentHistory,
  currentThread,
  isHistory,
  threadHistory,
  valueChanges,
} from "./history";

const tags: Tag[] = [
  { name: "A", kind: "project" },
  { name: "B", kind: "project" },
  { name: "공통", kind: "area" },
];
function row(id: string, seq: number, changes: Partial<Thread> = {}): Thread {
  return {
    thread_id: id,
    seq,
    kind: "기록",
    content: id,
    occurred_at: `2026-11-17T${String(seq).padStart(2, "0")}:00:00Z`,
    recorded_at: `2026-11-17T${String(seq).padStart(2, "0")}:00:00Z`,
    tags: ["A", "공통"],
    counterpart: null,
    source_id: "synthetic",
    actor: "human",
    field: "deadline",
    value: "2026-11-20",
    direction: null,
    resolves: null,
    retracts: null,
    updates: null,
    blocked_by: null,
    executes: null,
    correction: null,
    ...changes,
  };
}
describe("fact and target history projection", () => {
  it("honors API presentation and recognizes legacy histories when absent", () => {
    expect(isHistory(row("wiki:old", 1, { content: "deadline 변경" }))).toBe(
      true,
    );
    expect(isHistory(row("wiki:human", 1, { content: "의미 있는 사실" }))).toBe(
      false,
    );
    expect(isHistory(row("wiki-field:old", 1))).toBe(true);
    expect(
      isHistory(
        row("resolved-fact", 1, { presentation: "fact", resolves: "request" }),
      ),
    ).toBe(false);
    expect(isHistory(row("reject", 1, { kind: "거절" }))).toBe(true);
  });
  it("keeps field state current and rolls back retracted changes without mixing projects in one area", () => {
    const original = row("original", 1);
    const unrelated = row("unrelated", 2, {
      tags: ["B", "공통"],
      value: "2026-12-31",
    });
    const change = row("wiki-field:change", 3, {
      presentation: "history",
      value: "2026-11-25",
    });
    const undo = row("undo", 4, {
      field: null,
      value: null,
      retracts: change.thread_id,
    });
    const snapshot = [original, unrelated, change];
    expect(currentThread(original, snapshot, tags).value).toBe("2026-11-25");
    expect(valueChanges([change], snapshot, tags)[0].label).toBe(
      "마감 11/20 → 11/25",
    );
    expect(currentThread(original, [...snapshot, undo], tags).value).toBe(
      "2026-11-20",
    );
    expect(valueChanges([change], [...snapshot, undo], tags)[0].label).toBe(
      "마감 11/20 → 11/25 · 철회됨",
    );
  });
  it("folds corrections, executions and retractions of history into the target in chronological order", () => {
    const fact = row("fact", 1);
    const correction = row("correction", 2, {
      kind: "태그정정",
      field: null,
      correction: { target: "fact", add: ["태그"], remove: [] },
    });
    const execution = row("execute", 3, {
      kind: "실행",
      field: null,
      executes: "fact",
    });
    const undo = row("undo", 4, { field: null, retracts: "correction" });
    expect(
      threadHistory(fact, [fact, undo, execution, correction]).map(
        (t) => t.thread_id,
      ),
    ).toEqual(["correction", "execute", "undo"]);
    expect(
      adjacentHistory([fact, row("other", 5), undo, correction, execution]).map(
        (t) => t.thread_id,
      ),
    ).toEqual(["fact", "correction", "undo", "execute", "other"]);
  });
});
