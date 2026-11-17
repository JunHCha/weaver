import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, type FabricData, type Thread } from "../lib/api";
import Workspace from "./Workspace";
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
it("refreshes Fabric while visible and preserves filters, selection, focus and scroll", async () => {
  vi.useFakeTimers();
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  const threads: Thread[] = Array.from({ length: 8 }, (_, i) => ({
    thread_id: "t" + i,
    seq: i + 1,
    kind: "기록",
    content: "내용 " + i,
    occurred_at: "2026-11-15T09:00:00+09:00",
    recorded_at: "2026-11-15T09:00:00+09:00",
    tags: ["프로젝트"],
    source_id: "source",
    actor: "human",
    field: null,
    value: null,
    direction: null,
    resolves: null,
    retracts: null,
    executes: null,
    updates: null,
    blocked_by: null,
    correction: null,
  }));
  const data: FabricData = {
    threads,
    tags: [{ name: "프로젝트", kind: "project" }],
    sources: [],
    states: {},
    status: {
      spinner: { last_activity: null, waiting_review: 0 },
      loom: { last_activity: null, waiting_review: 0 },
    },
  };
  vi.spyOn(api, "fabric").mockResolvedValue(data);
  const refresh = vi
    .spyOn(api, "threads")
    .mockResolvedValue([
      ...threads,
      { ...threads[7], thread_id: "new", seq: 9, content: "내용 새로 추가" },
    ]);
  vi.spyOn(api, "tags").mockResolvedValue(data.tags);
  vi.spyOn(api, "states").mockResolvedValue({});
  const app = render(<Workspace />);
  await act(async () => {
    await Promise.resolve();
  });
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "project" },
  });
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "내용" },
  });
  fireEvent.click(screen.getByRole("article", { name: "기록 내용 7" }));
  const scroll = document.getElementById("scr")!;
  scroll.scrollTop = 137;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30000);
  });
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("묶기")).toHaveValue("project");
  expect(screen.getByRole("searchbox")).toHaveValue("내용");
  expect(screen.getByRole("button", { name: "전체 보기" })).toBeVisible();
  expect(document.querySelector(".card.sel")).toHaveAttribute("data-id", "t7");
  expect(
    screen.getByRole("article", { name: "기록 내용 새로 추가" }),
  ).toBeVisible();
  expect(scroll.scrollTop).toBe(137);
  expect(screen.getByText("0초 전 갱신")).toBeVisible();
  hidden.mockReturnValue(true);
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60000);
  });
  expect(refresh).toHaveBeenCalledTimes(1);
  hidden.mockReturnValue(false);
  await act(async () => {
    fireEvent(document, new Event("visibilitychange"));
  });
  expect(refresh).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "전체 보기" }));
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30000);
  });
  expect(screen.getByRole("button", { name: "로그" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  app.unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it("retains an open change list, selected history and filters across a poll", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-11-18T00:00:00+09:00"));
  const { fixture } = await import("../test/fabricFixture");
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  vi.spyOn(api, "fabric").mockResolvedValue(fixture);
  vi.spyOn(api, "threads").mockResolvedValue([...fixture.threads]);
  vi.spyOn(api, "tags").mockResolvedValue([...fixture.tags]);
  vi.spyOn(api, "states").mockResolvedValue({ ...fixture.states });
  const app = render(<Workspace />);
  await act(async () => {
    await Promise.resolve();
  });
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "project" },
  });
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "합성" },
  });
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  fireEvent.click(screen.getByText("변경 2"));
  fireEvent.click(screen.getByRole("button", { name: "마감 11/20 → 11/25" }));
  const details = screen.getByText("변경 2").closest("details")!;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30000);
  });
  expect(screen.getByText("변경 2").closest("details")).toBe(details);
  expect(details).toHaveAttribute("open");
  expect(screen.getByRole("searchbox")).toHaveValue("합성");
  expect(screen.getByLabelText("묶기")).toHaveValue("project");
  expect(
    screen.getByRole("complementary", { name: "Thread 상세" }),
  ).toHaveTextContent("wiki-field:deadline");
  app.unmount();
});
it("retains a collapsed counterpart-none column, selection and filters across a poll", async () => {
  vi.useFakeTimers();
  const { fixture } = await import("../test/fabricFixture");
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  vi.spyOn(api, "fabric").mockResolvedValue(fixture);
  vi.spyOn(api, "threads").mockResolvedValue([...fixture.threads]);
  vi.spyOn(api, "tags").mockResolvedValue([...fixture.tags]);
  vi.spyOn(api, "states").mockResolvedValue({ ...fixture.states });
  const app = render(<Workspace />);
  await act(async () => {
    await Promise.resolve();
  });
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "person" },
  });
  fireEvent.change(screen.getByRole("searchbox"), {
    target: { value: "합성" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "상대 없음 접기/펼치기" }),
  );
  fireEvent.click(screen.getByRole("article", { name: "요청 합성 fact" }));
  const lane = [...document.querySelectorAll(".lane-h")].find((el) =>
    el.textContent?.includes("상대 없음"),
  )!;
  expect(lane).toHaveClass("col");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30000);
  });
  expect(
    [...document.querySelectorAll(".lane-h")].find((el) =>
      el.textContent?.includes("상대 없음"),
    ),
  ).toHaveClass("col");
  expect(screen.getByRole("searchbox")).toHaveValue("합성");
  expect(document.querySelector(".card.sel")).toHaveAttribute(
    "data-id",
    "fact",
  );
  expect(screen.getByRole("button", { name: "전체 보기" })).toBeVisible();
  app.unmount();
});
