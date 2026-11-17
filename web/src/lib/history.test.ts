import { describe, expect, it } from "vitest";
import type { Tag, Thread } from "./api";
import {
  adjacentHistory,
  createThreadView,
  stateTag,
  mentionOnly,
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
  it("preserves each fact value and scopes field history without mixing projects in one area", () => {
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
    expect(createThreadView(snapshot, tags).fields.get("original")).toEqual([
      "마감",
      "11/20",
    ]);
    expect(valueChanges([change], snapshot, tags)[0].label).toBe(
      "마감 11/20 → 11/25",
    );
    expect(
      createThreadView([...snapshot, undo], tags).fields.get("original"),
    ).toEqual(["마감", "11/20"]);
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

it("does not use a person as a value target and treats mixed mention matches correctly", () => {
  const peopleTags: Tag[] = [
    { name: "민수", kind: "person" },
    { name: "회사", kind: "organization" },
    { name: "프로젝트", kind: "project" },
  ];
  const fact = row("one", 1, {
    tags: ["민수", "회사", "프로젝트"],
    counterpart: "다른 상대",
  });
  expect(
    stateTag({ ...fact, tags: ["민수", "회사"] }, peopleTags),
  ).toBeUndefined();
  expect(mentionOnly(fact, ["민수"], peopleTags)).toBe(true);
  expect(mentionOnly(fact, ["회사"], peopleTags)).toBe(true);
  expect(mentionOnly(fact, ["민수", "회사"], peopleTags)).toBe(true);
  expect(mentionOnly(fact, ["민수", "프로젝트"], peopleTags)).toBe(false);
  expect(
    mentionOnly({ ...fact, counterpart: "회사" }, ["민수", "회사"], peopleTags),
  ).toBe(false);
  expect(mentionOnly(fact, ["민수", "일치하지 않음"], peopleTags)).toBe(true);
});
it("bounds cyclic and deep history traversal without recursive stack growth", () => {
  const root = row("root", 1);
  const chain = Array.from({ length: 3000 }, (_, i) =>
    row("h" + i, 2, {
      field: null,
      kind: "태그정정",
      correction: { target: i ? "h" + (i - 1) : "root", add: [], remove: [] },
    }),
  );
  const snapshot = [root, ...chain];
  expect(threadHistory(root, snapshot)).toHaveLength(3000);
  expect(adjacentHistory(snapshot)).toHaveLength(3001);
  const cyclic = [
    row("x", 1, { retracts: "y" }),
    row("y", 2, { retracts: "x" }),
  ];
  expect(adjacentHistory(cyclic)).toHaveLength(2);
  expect(createThreadView(cyclic, tags).targets.size).toBe(2);
});
