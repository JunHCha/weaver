import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { FabricData, Thread } from "../lib/api";
import { FabricScreen } from "./FabricScreen";
const threads: Thread[] = Array.from({ length: 12 }, (_, i) => ({
  thread_id: "thread-" + i,
  seq: i + 1,
  kind: i === 0 ? "요청" : "기록",
  content: "내용 " + i,
  occurred_at: `2026-11-15T09:${String(i).padStart(2, "0")}:00+09:00`,
  recorded_at: `2026-11-15T09:${String(i).padStart(2, "0")}:00+09:00`,
  tags: i === 11 ? ["미정"] : [i % 2 ? "B" : "A", "사람"],
  source_id: i < 4 ? "source-1" : "source-2",
  actor: "human",
  field: null,
  value: null,
  direction: i === 0 ? "받음" : null,
  resolves: null,
  retracts: null,
  executes: null,
  updates: null,
  blocked_by: null,
  correction: null,
}));
const data: FabricData = {
  threads,
  tags: [
    { name: "A", kind: "project" },
    { name: "B", kind: "project" },
    { name: "사람", kind: "person" },
    { name: "미정", kind: null },
  ],
  sources: [],
  states: {
    A: {
      deadline: "2026-11-20",
      schedule: null,
      status: "진행중",
      assignees: ["담당자"],
      open_requests: ["thread-0"],
    },
  },
  status: {
    spinner: { last_activity: null, waiting_review: 0 },
    loom: { last_activity: null, waiting_review: 0 },
  },
};
function board() {
  return document.querySelector("#cg")!;
}
describe("Fabric React screens", () => {
  it("renders columns when the grouping switch changes, excluding unknown tag kinds", () => {
    render(<FabricScreen data={data} />);
    fireEvent.change(screen.getByLabelText("묶기"), {
      target: { value: "project" },
    });
    expect(
      [...document.querySelectorAll(".lane-h b")].map((n) => n.textContent),
    ).toEqual(["A", "B", "프로젝트 없음"]);
    fireEvent.change(screen.getByLabelText("묶기"), {
      target: { value: "person" },
    });
    expect(
      [...document.querySelectorAll(".lane-h b")].map((n) => n.textContent),
    ).toEqual(["사람", "사람 없음"]);
  });
  it("dims other cards on hover and keeps immediate global neighbors", () => {
    render(<FabricScreen data={data} />);
    fireEvent.mouseEnter(screen.getByRole("article", { name: "기록 내용 5" }));
    expect(board()).toHaveClass("dimming");
    expect(screen.getByRole("article", { name: "기록 내용 5" })).toHaveClass(
      "keep",
    );
    expect(screen.getByRole("article", { name: "기록 내용 4" })).toHaveClass(
      "keep",
    );
    expect(
      screen.getByRole("article", { name: "기록 내용 2" }),
    ).not.toHaveClass("keep");
    fireEvent.mouseLeave(screen.getByRole("article", { name: "기록 내용 5" }));
    expect(board()).not.toHaveClass("dimming");
  });
  it("focuses clicked + four previous + all later, and exits with the button or Escape", () => {
    render(<FabricScreen data={data} />);
    fireEvent.click(screen.getByRole("article", { name: "기록 내용 7" }));
    expect(
      [...board().querySelectorAll(".card:not(.ghost)")].map((n) =>
        n.getAttribute("data-id"),
      ),
    ).toEqual(threads.slice(3).map((t) => t.thread_id));
    expect(screen.getByText(/부터 보는 중 \(이전 4개 포함\)/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "전체 보기" }));
    expect(board().querySelectorAll(".card:not(.ghost)")).toHaveLength(12);
    fireEvent.click(screen.getByRole("article", { name: "기록 내용 7" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(board().querySelectorAll(".card:not(.ghost)")).toHaveLength(12);
  });
  it("shows properties, copyable IDs and the same-source list in the detail panel", () => {
    render(<FabricScreen data={data} />);
    fireEvent.click(screen.getByRole("article", { name: "기록 내용 2" }));
    const panel = within(
      screen.getByRole("complementary", { name: "Thread 상세" }),
    );
    expect(panel.getByText("thread-2")).toBeVisible();
    expect(panel.getByText("source-1")).toBeVisible();
    expect(panel.getAllByRole("button", { name: "복사" })).toHaveLength(2);
    expect(panel.getByRole("heading", { name: /같은 출처/ })).toBeVisible();
    expect(document.querySelectorAll(".samelist button")).toHaveLength(4);
  });
  it("resets an empty filtered result", () => {
    render(<FabricScreen data={data} />);
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "없음없는검색어" },
    });
    expect(
      screen.getByText("조건에 맞는 Thread가 없습니다. 필터를 줄여 보세요."),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "필터 초기화" }));
    expect(board().querySelectorAll(".card:not(.ghost)")).toHaveLength(12);
  });
  it("renders sticky log sections with authoritative state summaries and a time spine", () => {
    render(<FabricScreen data={data} />);
    fireEvent.change(screen.getByLabelText("묶기"), {
      target: { value: "project" },
    });
    fireEvent.click(screen.getByRole("button", { name: "로그" }));
    const section = document.querySelector(".lsech")!;
    expect(section).toHaveTextContent("마감 11/20");
    expect(section).toHaveTextContent("상태 진행중");
    expect(section).toHaveTextContent("담당자 담당자");
    expect(section).toHaveTextContent("열린 요청 1");
    expect(document.querySelectorAll(".lsvg circle")).toHaveLength(12);
  });
});


it("hides archived project groups until requested and keeps the area view intact", () => {
  render(<FabricScreen data={{ ...data, tags: data.tags.map((tag) => ({ ...tag, archived: tag.name === "A" })) }} />);
  fireEvent.change(screen.getByLabelText("묶기"), { target: { value: "project" } });
  const names = () => [...document.querySelectorAll(".lane-h b")].map((n) => n.textContent);
  expect(names()).toEqual(["B", "프로젝트 없음"]);
  expect(screen.getByRole("article", { name: "기록 내용 2" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "보관 프로젝트 표시" }));
  expect(names()).toEqual(["A", "B", "프로젝트 없음"]);
  fireEvent.change(screen.getByLabelText("묶기"), { target: { value: "none" } });
  expect(board().querySelectorAll(".card:not(.ghost)")).toHaveLength(12);
});
