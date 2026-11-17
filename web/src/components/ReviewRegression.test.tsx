import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { editing } from "../lib/api";
import { fixture, thread } from "../test/fabricFixture";
import { FabricScreen } from "./FabricScreen";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-18T00:00:00+09:00"));
  vi.spyOn(editing, "dictionary").mockResolvedValue([]);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
function group(value: string) {
  fireEvent.change(screen.getByLabelText("묶기"), { target: { value } });
}
function tag(name: string) {
  fireEvent.click(screen.getByRole("button", { name: /^태그/ }));
  fireEvent.click(screen.getByRole("checkbox", { name }));
}
function filters() {
  group("project");
  tag("A");
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "합성" },
  });
  fireEvent.click(screen.getByRole("button", { name: "기간 전체" }));
  fireEvent.click(screen.getByRole("radio", { name: "지난 7일" }));
}
function retained() {
  expect(screen.getByLabelText("묶기")).toHaveValue("project");
  expect(screen.getByRole("searchbox")).toHaveValue("합성");
  expect(screen.getByRole("button", { name: "태그 1" })).toBeVisible();
  expect(screen.getByRole("button", { name: "기간 지난 7일" })).toBeVisible();
  expect(screen.getByRole("button", { name: "이력 포함" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  expect(
    screen.getByRole("button", { name: "보관 프로젝트 표시" }),
  ).toHaveAttribute("aria-pressed", "false");
}
it("opens a value dot and an inline history item without resetting any user filters", () => {
  render(<FabricScreen data={fixture} />);
  filters();
  fireEvent.click(screen.getByRole("button", { name: "마감 11/20 → 11/25" }));
  retained();
  expect(
    screen.getByRole("complementary", { name: "Thread 상세" }),
  ).toHaveTextContent("wiki-field:deadline");
  fireEvent.click(screen.getByRole("button", { name: "닫기" }));
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  fireEvent.click(screen.getByText("변경 2"));
  fireEvent.click(screen.getByRole("button", { name: "마감 11/20 → 11/25" }));
  retained();
  expect(screen.getByText("변경 2").closest("details")).toHaveAttribute("open");
});
it.each(["none", "person", "organization", "kind", "source", "topic"])(
  "does not attach value changes or create columns in %s grouping",
  (value) => {
    render(<FabricScreen data={fixture} />);
    group(value);
    expect(document.querySelectorAll(".history-dot")).toHaveLength(0);
    expect(
      [...document.querySelectorAll(".lane-h b")].map((el) => el.textContent),
    ).not.toContain("B");
    expect(document.querySelectorAll(".card:not(.ghost)")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "로그" }));
    expect(screen.getByRole("region", { name: "값 변경 이력" })).toBeVisible();
    expect(document.querySelectorAll(".lsec .value-history")).toHaveLength(0);
    expect(screen.getAllByText("변경 3")).toHaveLength(1);
    expect(document.body).not.toHaveTextContent("오염된 사람 상태");
  },
);
it("does not create an orphan field-history column and preserves own values and scoped detail history", () => {
  render(<FabricScreen data={fixture} />);
  group("project");
  expect(
    [...document.querySelectorAll(".lane-h b")].map((el) => el.textContent),
  ).toEqual(["A", "프로젝트 없음"]);
  const card = screen.getByRole("article", { name: "요청 합성 fact" });
  expect(card).toHaveTextContent("마감11/20");
  expect(card).not.toHaveTextContent("11/25");
  expect(
    screen.getByRole("article", { name: "기록 합성 none" }),
  ).toHaveTextContent("12/1");
  fireEvent.click(card);
  const panel = within(
    screen.getByRole("complementary", { name: "Thread 상세" }),
  );
  const history = panel.getByRole("list", { name: "이력" });
  expect(history).toHaveTextContent("마감 11/20 → 11/25");
  expect(history).not.toHaveTextContent("진행중");
  expect(history).toHaveTextContent("합성 exec");
  fireEvent.click(screen.getByRole("button", { name: "닫기" }));
  fireEvent.click(screen.getByRole("article", { name: "기록 합성 mention" }));
  expect(
    within(
      screen.getByRole("complementary", { name: "Thread 상세" }),
    ).queryByRole("list", { name: "이력" }),
  ).not.toBeInTheDocument();
});
it("expands changes inline and closes them with Escape and outside pointer", () => {
  render(<FabricScreen data={fixture} />);
  group("project");
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  const summary = screen.getByText("변경 2"),
    details = summary.closest("details")!;
  fireEvent.click(summary);
  expect(details).toHaveAttribute("open");
  expect(details.querySelector("ul")).toBeVisible();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(details).not.toHaveAttribute("open");
  expect(summary).toHaveFocus();
  fireEvent.click(summary);
  fireEvent.pointerDown(screen.getByRole("heading", { name: "Fabric" }));
  expect(details).not.toHaveAttribute("open");
});
it("uses tag archive metadata, routes all-hidden projects to the none lane, and keeps facts when history is disabled", () => {
  const data = {
    ...fixture,
    tags: fixture.tags.map((t) => ({ ...t, archived: t.name === "A" })),
    threads: [
      ...fixture.threads,
      thread("archived-status", {
        tags: ["B"],
        field: "status",
        value: "archived",
      }),
    ],
  };
  render(<FabricScreen data={data} />);
  group("project");
  expect(
    [...document.querySelectorAll(".lane-h b")].map((el) => el.textContent),
  ).toEqual(["B", "프로젝트 없음"]);
  expect(
    screen.getByRole("article", { name: "요청 합성 fact" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "보관 프로젝트 표시" }));
  expect(
    [...document.querySelectorAll(".lane-h b")].map((el) => el.textContent),
  ).toContain("A");
  const before = [...document.querySelectorAll(".card:not(.ghost)")].map((el) =>
    el.getAttribute("data-id"),
  );
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  expect(
    screen.getByRole("article", { name: "실행 합성 exec · 이력" }),
  ).toHaveTextContent("completed");
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  expect(
    [...document.querySelectorAll(".card:not(.ghost)")].map((el) =>
      el.getAttribute("data-id"),
    ),
  ).toEqual(before);
});

it("shows each person-only Thread's own value even when a later Thread shares that person", () => {
  render(
    <FabricScreen
      data={{
        ...fixture,
        threads: [
          fixture.threads[2],
          thread("later-person", {
            seq: 9,
            tags: ["민수"],
            field: "deadline",
            value: "2026-12-10",
          }),
        ],
      }}
    />,
  );
  expect(
    screen.getByRole("article", { name: "기록 합성 none" }),
  ).toHaveTextContent("마감12/1");
  expect(
    screen.getByRole("article", { name: "기록 합성 later-person" }),
  ).toHaveTextContent("마감12/10");
});
it("marks an organization-only mention match but removes the mark when a project also matches", () => {
  render(<FabricScreen data={fixture} />);
  tag("회사");
  expect(
    screen.getByRole("article", { name: "기록 합성 mention · 언급" }),
  ).toHaveClass("mention-only");
  tag("A");
  expect(
    screen.getByRole("article", { name: "기록 합성 mention" }),
  ).not.toHaveClass("mention-only");
});
it("opens an ordinary filtered-out fact without enabling archives and enables only archives for an archived fact", () => {
  const data = {
    ...fixture,
    threads: [
      ...fixture.threads,
      thread("archived-fact", { seq: 9, tags: ["B"] }),
    ],
    tags: fixture.tags.map((t) => ({ ...t, archived: t.name === "B" })),
  };
  render(<FabricScreen data={data} />);
  group("project");
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "합성 fact" },
  });
  fireEvent.click(screen.getByRole("article", { name: "요청 합성 fact" }));
  const panel = within(
    screen.getByRole("complementary", { name: "Thread 상세" }),
  );
  fireEvent.click(panel.getByRole("button", { name: /합성 mention/ }));
  expect(screen.getByRole("searchbox")).toHaveValue("합성 fact");
  expect(
    screen.getByRole("button", { name: "보관 프로젝트 표시" }),
  ).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(
    panel.getByRole("button", { name: /합성 archived-fact/ }),
  );
  expect(screen.getByRole("searchbox")).toHaveValue("합성 fact");
  expect(
    screen.getByRole("button", { name: "보관 프로젝트 표시" }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "이력 포함" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});
