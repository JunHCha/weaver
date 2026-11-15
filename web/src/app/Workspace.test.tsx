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
