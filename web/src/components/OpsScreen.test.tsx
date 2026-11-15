import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, type Status } from "../lib/api";
import { OpsScreen } from "./OpsScreen";
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
it("refreshes every five seconds, applies server log filters and cleans up on unmount", async () => {
  vi.useFakeTimers();
  const status: Status = {
    spinner: { last_activity: null, waiting_review: 0 },
    loom: { last_activity: null, waiting_review: 2 },
  };
  const sources = vi
    .spyOn(api, "sources")
    .mockResolvedValue([
      {
        id: "source-1",
        kind: "통화",
        status: "완료",
        updated_at: "2026-11-15T09:00:00+09:00",
        stage_times: {},
      },
    ]);
  const logs = vi
    .spyOn(api, "logs")
    .mockResolvedValue([
      {
        ts: "2026-11-15 09:00:00",
        lv: "INFO",
        lg: "weaver.spinner",
        msg: "합성 로그",
        src: "source-1",
      },
    ]);
  vi.spyOn(api, "status").mockResolvedValue(status);
  const onStatus = vi.fn();
  const result = render(
    <OpsScreen stage="spinner" status={status} onStatus={onStatus} />,
  );
  await act(async () => {
    await Promise.resolve();
  });
  expect(screen.getByText("합성 로그", { exact: false })).toBeVisible();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "WARNING" }));
  });
  expect(logs).toHaveBeenLastCalledWith(
    "spinner",
    "WARNING",
    "",
    expect.any(AbortSignal),
  );
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /통화/ }));
  });
  expect(logs).toHaveBeenLastCalledWith(
    "spinner",
    "WARNING",
    "source-1",
    expect.any(AbortSignal),
  );
  const before = sources.mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(sources).toHaveBeenCalledTimes(before + 1);
  result.unmount();
  expect(vi.getTimerCount()).toBe(0);
});
