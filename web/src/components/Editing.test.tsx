import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  editing,
  type DictionaryEntry,
  type FabricData,
  type Thread,
} from "../lib/api";
import { TagEditor } from "./TagEditor";
import { DictionaryScreen } from "./DictionaryScreen";
import { FabricScreen } from "./FabricScreen";
const entry: DictionaryEntry = {
  name: "알파",
  kind: "project",
  aliases: ["Alpha"],
  usage_count: 2,
  human_edited: true,
};
const thread: Thread = {
  thread_id: "one",
  seq: 1,
  kind: "기록",
  content: "원래 기록",
  tags: ["알파"],
  occurred_at: "2026-11-16T12:00:00+09:00",
  recorded_at: "2026-11-16T12:00:00+09:00",
  source_id: "synthetic",
  actor: "human",
  field: null,
  value: null,
  direction: null,
  resolves: null,
  retracts: null,
  updates: null,
  executes: null,
  blocked_by: null,
  correction: null,
};
afterEach(() => vi.restoreAllMocks());
describe("tag editing", () => {
  it("autocompletes aliases and requires a kind only for a new name", async () => {
    vi.spyOn(editing, "dictionary").mockResolvedValue([entry]);
    const save = vi.spyOn(editing, "tags").mockResolvedValue();
    const onSaved = vi.fn();
    render(<TagEditor thread={{ ...thread, tags: [] }} onSaved={onSaved} />);
    const input = screen.getByLabelText("추가할 태그");
    await waitFor(() => expect(input).toBeEnabled());
    expect(document.querySelector('option[value="Alpha"]')).toHaveTextContent(
      "알파",
    );
    fireEvent.change(input, { target: { value: "Alpha" } });
    expect(screen.queryByLabelText("새 태그 종류")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "추가" }));
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith("one", {
        add: ["알파"],
        remove: [],
        kinds: {},
      }),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    fireEvent.change(input, { target: { value: "새 이름" } });
    expect(screen.getByRole("button", { name: "추가" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("새 태그 종류"), {
      target: { value: "person" },
    });
    fireEvent.click(screen.getByRole("button", { name: "추가" }));
    await waitFor(() =>
      expect(save).toHaveBeenLastCalledWith("one", {
        add: ["새 이름"],
        remove: [],
        kinds: { "새 이름": "person" },
      }),
    );
  });
  it("removes optimistically and rolls back on rejection", async () => {
    vi.spyOn(editing, "dictionary").mockResolvedValue([entry]);
    let reject!: (error: Error) => void;
    vi.spyOn(editing, "tags").mockImplementation(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    );
    render(<TagEditor thread={thread} />);
    fireEvent.click(screen.getByRole("button", { name: "알파 태그 삭제" }));
    expect(screen.queryByText("#알파")).not.toBeInTheDocument();
    await act(async () => reject(new Error("잠시 후 다시 시도하세요")));
    expect(screen.getByText("#알파")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "잠시 후 다시 시도하세요",
    );
  });
});
describe("dictionary screen", () => {
  it("groups, searches aliases, edits names/kinds/aliases and confirms a merge", async () => {
    const beta: DictionaryEntry = {
      ...entry,
      name: "베타",
      aliases: [],
      kind: "person",
      usage_count: 0,
    };
    vi.spyOn(editing, "dictionary").mockResolvedValue([entry, beta]);
    const rename = vi.spyOn(editing, "entry").mockResolvedValue();
    const alias = vi.spyOn(editing, "alias").mockResolvedValue();
    const merge = vi.spyOn(editing, "merge").mockResolvedValue();
    render(<DictionaryScreen />);
    const row = await screen.findByRole("article", { name: "알파" });
    expect(within(row).getByText("Thread 2개")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("사전 검색"), {
      target: { value: "Alpha" },
    });
    expect(
      screen.queryByRole("article", { name: "베타" }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("사전 검색"), {
      target: { value: "" },
    });
    fireEvent.change(within(row).getByLabelText("알파 이름"), {
      target: { value: "새 이름" },
    });
    fireEvent.click(within(row).getByText("이름 저장"));
    await waitFor(() =>
      expect(rename).toHaveBeenCalledWith("알파", { name: "새 이름" }),
    );
    await waitFor(() =>
      expect(screen.queryByText("저장 중…")).not.toBeInTheDocument(),
    );
    fireEvent.change(within(row).getByLabelText("알파 종류"), {
      target: { value: "topic" },
    });
    await waitFor(() =>
      expect(rename).toHaveBeenLastCalledWith("알파", { kind: "topic" }),
    );
    await waitFor(() =>
      expect(screen.queryByText("저장 중…")).not.toBeInTheDocument(),
    );
    fireEvent.change(within(row).getByLabelText("알파 새 별칭"), {
      target: { value: "별명" },
    });
    fireEvent.click(within(row).getByText("별칭 추가"));
    await waitFor(() => expect(alias).toHaveBeenCalledWith("알파", "별명"));
    await waitFor(() =>
      expect(screen.queryByText("저장 중…")).not.toBeInTheDocument(),
    );
    fireEvent.click(within(row).getByLabelText("Alpha 별칭 삭제"));
    await waitFor(() =>
      expect(alias).toHaveBeenLastCalledWith("알파", "Alpha", true),
    );
    await waitFor(() =>
      expect(screen.queryByText("저장 중…")).not.toBeInTheDocument(),
    );
    fireEvent.change(within(row).getByLabelText("알파 합칠 대상"), {
      target: { value: "베타" },
    });
    fireEvent.click(within(row).getByText("합치기"));
    expect(merge).not.toHaveBeenCalled();
    fireEvent.click(within(row).getByRole("button", { name: "합치기 확인" }));
    await waitFor(() => expect(merge).toHaveBeenCalledWith("알파", "베타"));
  });
  it("shows empty copy and write failures", async () => {
    vi.spyOn(editing, "dictionary").mockResolvedValueOnce([]);
    const { unmount } = render(<DictionaryScreen />);
    expect(
      await screen.findByText(/아직 등록된 태그가 없습니다/),
    ).toBeInTheDocument();
    unmount();
    vi.spyOn(editing, "dictionary").mockResolvedValue([entry]);
    vi.spyOn(editing, "entry").mockRejectedValue(new Error("이름 충돌"));
    render(<DictionaryScreen />);
    await screen.findByRole("article", { name: "알파" });
    fireEvent.change(screen.getByLabelText("알파 종류"), {
      target: { value: "topic" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("이름 충돌");
    expect(screen.getByLabelText("알파 종류")).toHaveValue("project");
  });
});
it("hides retraction pairs by default and places retractions under the target in log groups", () => {
  const retract = {
    ...thread,
    thread_id: "undo",
    seq: 3,
    content: "철회 기록",
    tags: ["다른 태그"],
    retracts: "one",
  };
  const middle = { ...thread, thread_id: "two", seq: 2, content: "유지 기록" };
  const data: FabricData = {
    threads: [thread, middle, retract],
    tags: [{ name: "알파", kind: "project" }],
    states: {},
    sources: [],
    status: {
      spinner: { last_activity: null, waiting_review: 0 },
      loom: { last_activity: null, waiting_review: 0 },
    },
  };
  render(<FabricScreen data={data} />);
  expect(
    screen.queryByRole("article", { name: "기록 원래 기록" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("article", { name: "기록 철회 기록" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "철회 포함" }));
  expect(
    screen.getByRole("article", { name: "기록 원래 기록" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("article", { name: "기록 철회 기록" }),
  ).toBeInTheDocument();
  expect(document.querySelector("[data-retraction=undo]")).toHaveAttribute(
    "stroke-dasharray",
    "5 4",
  );
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  fireEvent.change(screen.getByLabelText("묶기"), {
    target: { value: "project" },
  });
  expect(
    [...document.querySelectorAll(".lrow[data-id]")].map((n) =>
      n.getAttribute("data-id"),
    ),
  ).toEqual(["one", "undo", "two"]);
  fireEvent.click(screen.getByRole("button", { name: "철회 포함" }));
  expect(
    [...document.querySelectorAll(".lrow[data-id]")].map((n) =>
      n.getAttribute("data-id"),
    ),
  ).toEqual(["two"]);
});
