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
  counterpart: i === 11 ? null : "사람",
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
    ).toEqual(["사람", "상대 없음"]);
  });
  it("keeps shared counterparts and mentions highlighted on hover", () => {
    render(<FabricScreen data={data} />);
    fireEvent.mouseEnter(screen.getByRole("article", { name: "기록 내용 5" }));
    expect(board()).toHaveClass("dimming");
    expect(screen.getByRole("article", { name: "기록 내용 5" })).toHaveClass(
      "keep",
    );
    expect(screen.getByRole("article", { name: "기록 내용 4" })).toHaveClass(
      "keep",
    );
    expect(screen.getByRole("article", { name: "기록 내용 2" })).toHaveClass(
      "keep",
    );
    expect(
      screen.getByRole("article", { name: "기록 내용 11" }),
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

const synthetic: FabricData = {
  ...data,
  tags: [
    { name: "진행 프로젝트", kind: "project" },
    { name: "옛 프로젝트", kind: "project", archived: true },
    { name: "제품", kind: "area" },
    { name: "민수", kind: "person" },
    { name: "지연", kind: "person" },
  ],
  threads: [
    {
      ...threads[0],
      thread_id: "fact",
      seq: 1,
      content: "합성 요청",
      counterpart: "민수",
      tags: ["진행 프로젝트", "제품", "민수", "지연"],
      field: "deadline",
      value: "2026-11-20",
      presentation: "fact",
    },
    {
      ...threads[1],
      thread_id: "mention",
      seq: 2,
      content: "합성 언급",
      counterpart: "지연",
      tags: ["진행 프로젝트", "제품", "민수", "지연"],
      presentation: "fact",
    },
    {
      ...threads[2],
      thread_id: "old",
      seq: 3,
      content: "합성 보관",
      counterpart: null,
      tags: ["옛 프로젝트"],
      presentation: "fact",
    },
    {
      ...threads[3],
      thread_id: "wiki-field:deadline",
      seq: 4,
      content: "마감 변경 합성 이력",
      counterpart: null,
      tags: ["진행 프로젝트", "제품"],
      field: "deadline",
      value: "2026-11-25",
      presentation: "history",
    },
    {
      ...threads[4],
      thread_id: "execution",
      seq: 5,
      kind: "실행",
      content: "실행 합성 이력",
      counterpart: null,
      tags: [],
      executes: "fact",
      presentation: "history",
    },
    {
      ...threads[5],
      thread_id: "correction",
      seq: 6,
      kind: "태그정정",
      content: "태그 합성 이력",
      counterpart: null,
      tags: [],
      correction: { target: "fact", add: ["제품"], remove: [] },
      presentation: "history",
    },
  ],
  states: {
    "진행 프로젝트": { ...data.states.A, deadline: "2026-11-25" },
    "옛 프로젝트": { ...data.states.A, status: "archived" },
  },
};
it("places each person card once by counterpart, exposes areas and marks mention-only matches", () => {
  render(<FabricScreen data={synthetic} />);
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "person" },
  });
  expect(
    [...document.querySelectorAll(".lane-h b")].map((n) => n.textContent),
  ).toEqual(["민수", "지연", "상대 없음"]);
  expect(board().querySelectorAll(".card:not(.ghost)")).toHaveLength(3);
  fireEvent.click(screen.getByRole("button", { name: "태그" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "민수" }));
  expect(screen.getByRole("article", { name: "기록 합성 언급 · 언급" })).toHaveClass(
    "mention-only",
  );
  expect(
    within(screen.getByRole("article", { name: "기록 합성 언급 · 언급" })).getByText(
      "언급",
    ),
  ).toBeVisible();
  expect(
    screen.getByRole("article", { name: "요청 합성 요청" }),
  ).not.toHaveClass("mention-only");
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "area" },
  });
  expect(document.querySelector(".lane-h b")).toHaveTextContent("제품");
});
it("hides archived project lanes until requested and preserves current values with folded changes", () => {
  render(<FabricScreen data={synthetic} />);
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "project" },
  });
  expect(document.querySelector(".lane-h")).not.toHaveTextContent(
    "옛 프로젝트",
  );
  expect(screen.getByRole("article", {name:"기록 합성 보관"})).toBeInTheDocument();
  expect(
    screen.getByRole("article", { name: "요청 합성 요청" }),
  ).toHaveTextContent("11/20");
  expect(
    screen.getByRole("button", { name: /마감 11\/20 → 11\/25/ }),
  ).toHaveClass("history-dot");
  fireEvent.click(screen.getByRole("button", { name: "보관 프로젝트 표시" }));
  expect(
    screen.getByRole("article", { name: "기록 합성 보관" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  const changes = screen.getByText("변경 1").closest("details")!;
  expect(changes).not.toHaveAttribute("open");
  fireEvent.click(screen.getByText("변경 1"));
  expect(changes).toHaveTextContent("마감 11/20 → 11/25");
  expect(document.querySelector(".lsech")).toHaveTextContent("마감 11/25");
  expect(
    document.querySelector('[data-id="wiki-field:deadline"]'),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  expect(document.querySelector('[data-id="execution"]')).toHaveClass(
    "history-item",
  );
  expect(
    document.querySelector('[data-id="wiki-field:deadline"]'),
  ).not.toBeInTheDocument();
  expect(
    [...document.querySelectorAll(".lrow[data-id]")].map((n) =>
      n.getAttribute("data-id"),
    ),
  ).toEqual(["fact", "execution", "correction", "mention", "old"]);
});
it("unifies target history and keeps counterpart separate from editable tags", () => {
  render(<FabricScreen data={synthetic} />);
  fireEvent.click(screen.getByRole("article", { name: "요청 합성 요청" }));
  const panel = within(
    screen.getByRole("complementary", { name: "Thread 상세" }),
  );
  expect(panel.getByText("상대").nextElementSibling).toHaveTextContent("민수");
  const history = panel.getByRole("list", { name: "이력" });
  expect(history).toHaveTextContent("실행 합성 이력");
  expect(history).toHaveTextContent("+제품");
  expect(history).toHaveTextContent("마감 11/20 → 11/25");
});

it("keeps a resolving fact in its own counterpart lane and nests retractions of corrections with the target", () => {
  const source: FabricData = {
    ...synthetic,
    threads: [
      synthetic.threads[0],
      { ...synthetic.threads[1], resolves: "fact" },
      synthetic.threads[5],
      {
        ...synthetic.threads[5],
        thread_id: "undo-correction",
        seq: 7,
        kind: "기록",
        content: "정정 철회",
        correction: null,
        retracts: "correction",
      },
    ],
  };
  render(<FabricScreen data={source} />);
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "person" },
  });
  expect(document.querySelectorAll(".lane-h")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "태그" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "민수" }));
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  const section = document.querySelector(".lsec")!;
  expect(
    [...section.querySelectorAll(".lrow[data-id]")].map((n) =>
      n.getAttribute("data-id"),
    ),
  ).toEqual(["fact", "correction", "undo-correction"]);
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
