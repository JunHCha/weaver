import { renderToString } from "react-dom/server";
import { expect, it, vi } from "vitest";
import * as presentation from "../lib/presentation";
import * as projection from "../lib/projection";
import { fixture, thread } from "../test/fabricFixture";
import { FabricScreen } from "./FabricScreen";

it("renders 3000 synthetic Threads with one relation index and no per-card state projections", () => {
  const threads = Array.from({ length: 3000 }, (_, i) =>
    thread("large-" + i, {
      seq: i + 1,
      field: "deadline",
      value: "2026-11-20",
      tags: ["A", "영역", "민수"],
      resolves: i % 3 === 0 ? "large-" + Math.max(0, i - 1) : null,
    }),
  );
  const relation = vi.spyOn(presentation, "relations"),
    project = vi.spyOn(projection, "projectRows");
  const started = performance.now();
  try {
    const html = renderToString(
      <FabricScreen data={{ ...fixture, threads }} />,
    );
    expect(html.match(/class="card /g) || []).toHaveLength(3000);
    expect(relation).toHaveBeenCalledTimes(1);
    expect(project).not.toHaveBeenCalled();
    // Generous wall-clock guard complements deterministic work-count assertions.
    expect(performance.now() - started).toBeLessThan(5000);
  } finally {
    vi.restoreAllMocks();
  }
});
