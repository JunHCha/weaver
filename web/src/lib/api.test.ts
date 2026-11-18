import { afterEach, expect, it, vi } from "vitest";
import { editing } from "./api";
afterEach(() => vi.unstubAllGlobals());
it("fetches one session token for the whole batch and uses the corrections undo route", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({ token: "synthetic-token" }),
    })
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        changed: 2,
        skipped: 0,
        skipped_threads: [],
        corrections: [],
      }),
    });
  vi.stubGlobal("fetch", fetch);
  expect(
    await editing.batchTags(["one", "two"], { add: [], remove: ["unknown"] }),
  ).toMatchObject({ changed: 2 });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][0]).toBe("/api/session");
  expect(fetch.mock.calls[1][0]).toBe("/api/threads/tags/batch");
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
    thread_ids: ["one", "two"],
    add: [],
    remove: ["unknown"],
  });
  expect(fetch.mock.calls[1][1].headers["X-Weaver-Token"]).toBe(
    "synthetic-token",
  );
  fetch
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({ token: "synthetic-token" }),
    })
    .mockResolvedValueOnce({ ok: true, json: async () => ({}) });
  await editing.undoTags("correction/1");
  expect(fetch.mock.calls[3][0]).toBe("/api/corrections/correction%2F1/undo");
});
