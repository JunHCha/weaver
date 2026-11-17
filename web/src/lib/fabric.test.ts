import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import {
  groupKeys,
  highlightedSegments,
  focusLayout,
  selectFocus,
  packColumns,
  weftPath,
  gapLabel,
  pulseDurations,
  stateSummary,
} from "./fabric";
import { generate } from "../../scripts/tokens.mjs";
const rows = Array.from({ length: 12 }, (_, i) => ({
  thread_id: String(i),
  occurred_at: `2026-11-15T00:${String(i).padStart(2, "0")}:00Z`,
  kind: "기록",
  tags: ["A", "B", "unknown"],
  source_id: "source",
}));
describe("Fabric geometry and selection", () => {
  it("packs each group from the top and duplicates independently", () => {
    expect(
      packColumns(
        [
          { lanes: [0, 1], heights: [50, 60] },
          { lanes: [1], heights: [40] },
          { lanes: [0], heights: [30] },
        ],
        2,
      ),
    ).toEqual({ positions: [[4, 4], [74], [64]], bottoms: [104, 124] });
  });
  it("includes four global predecessors and every successor", () => {
    expect([...selectFocus(rows, "7")!]).toEqual([
      "3",
      "4",
      "5",
      "6",
      "7",
      "8",
      "9",
      "10",
      "11",
    ]);
    expect([...selectFocus(rows, "1")!]).toHaveLength(12);
    expect(selectFocus(rows, "missing")).toBeNull();
  });
  it("uses a vertical offset in one lane and a curve across lanes", () => {
    const a = { x: 24, y: 18, lane: 0 };
    expect(weftPath(a, { x: 24, y: 100, lane: 0 })).toBe("M18 18L18 100");
    expect(weftPath(a, { x: 300, y: 80, lane: 1 })).toBe(
      "M24 18C40 18 284 80 300 80",
    );
  });
  it.each([
    [0, "같은 시각"],
    [5, "5분 뒤"],
    [90, "2시간 뒤"],
    [2880, "2일 뒤"],
  ])("labels %s minute gaps", (minutes, label) => {
    expect(
      gapLabel(
        "2026-01-01T00:00:00Z",
        new Date(Date.UTC(2026, 0, 1, 0, Number(minutes))).toISOString(),
      ),
    ).toBe(label);
  });
});
describe("Grouping and projected state", () => {
  it("uses only tags with the requested known kind", () => {
    const kinds = { A: "project", B: "person", C: "area", unknown: null };
    expect(groupKeys({ ...rows[0], tags: ["C"] }, "area", kinds, String)).toEqual(["C"]);
    expect(groupKeys(rows[0], "project", kinds, String)).toEqual(["A"]);
    expect(groupKeys(rows[0], "topic", kinds, String)).toEqual(["자료 없음"]);
    expect(
      groupKeys({ ...rows[0], tags: ["unknown"] }, "project", kinds, String),
    ).toEqual(["프로젝트 없음"]);
    expect(groupKeys(rows[0], "kind", kinds, String)).toEqual(["기록"]);
    expect(groupKeys(rows[0], "source", kinds, String)).toEqual(["source"]);
    expect(groupKeys(rows[0], "none", kinds, String)).toEqual([""]);
  });
  it("formats authoritative full-snapshot state, including zero requests", () => {
    expect(
      stateSummary({
        deadline: "2026-11-20",
        schedule: null,
        status: "완료",
        assignees: ["가", "나"],
        open_requests: [],
      }),
    ).toEqual([
      ["마감", "11/20"],
      ["상태", "완료"],
      ["담당자", "가, 나"],
      ["열린 요청", "0"],
    ]);
  });
  it("fails when generated design tokens drift", () => {
    expect(
      readFileSync(new URL("../styles/tokens.css", import.meta.url), "utf8"),
    ).toBe(generate());
    expect(
      readFileSync(new URL("../../public/logo.svg", import.meta.url), "utf8"),
    ).toBe(
      readFileSync(
        new URL("../../../design/logo.svg", import.meta.url),
        "utf8",
      ),
    );
  });
});

it("caps variable pulse hops with a 200ms floor", () => {
  for (const count of [1, 10, 29, 30, 50]) {
    const ms = pulseDurations(Array.from({ length: count }, (_, i) => i * 91));
    expect(ms.length).toBe(Math.min(count, 30));
    expect(Math.min(...ms)).toBeGreaterThanOrEqual(200);
    expect(ms.reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(6000.00001);
  }
});
it("formats schedules without a known end", () => {
  expect(
    stateSummary({
      deadline: null,
      schedule: {
        start: "2026-11-20T09:00:00+09:00",
        end: null,
        timezone: "Asia/Seoul",
      },
      status: null,
      assignees: [],
      open_requests: ["a"],
    })[0],
  ).toEqual(["일정", "11/20 09:00–—"]);
});

it("highlights only the two previous and two following chronological segments", () => {
  expect([
    ...highlightedSegments(
      rows.map((t) => t.thread_id),
      "5",
    ),
  ]).toEqual([3, 4, 5, 6]);
  expect([
    ...highlightedSegments(
      rows.map((t) => t.thread_id),
      "0",
    ),
  ]).toEqual([0, 1]);
  expect([
    ...highlightedSegments(
      rows.map((t) => t.thread_id),
      "missing",
    ),
  ]).toEqual([]);
});
it("lays out focused groups independently of FLIP animation and preserves top alignment", () => {
  const lanes = [
    { key: "A", count: 12, none: false },
    { key: "프로젝트 없음", count: 1, none: true },
  ];
  const all = focusLayout(
    rows,
    lanes,
    () => ["A"],
    null,
    false,
    1000,
    {},
    false,
  );
  const focused = focusLayout(
    rows,
    lanes,
    () => ["A"],
    "7",
    false,
    1000,
    {},
    false,
  );
  expect(all.cards[0].y).toBe(4);
  expect(focused.cards[0].id).toBe("3");
  expect(focused.cards[0].y).toBe(4);
  expect(focused.cards).toHaveLength(9);
  expect(focused.lanes[1].collapsed).toBe(true);
  expect(
    focusLayout(
      rows,
      lanes,
      () => ["프로젝트 없음"],
      null,
      true,
      1000,
      {},
      false,
    ).cards,
  ).toEqual([]);
});
