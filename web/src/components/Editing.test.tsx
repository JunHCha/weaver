import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  editing,
  type DictionaryEntry,
  type FabricData,
  type Thread,
} from "../lib/api";
import { initialFilters } from "./FabricToolbar";
import { TagEditor } from "./TagEditor";
import { DictionaryScreen } from "./DictionaryScreen";
import { FabricScreen, filteredThreads } from "./FabricScreen";
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
    fireEvent.click(within(row).getByRole("button", { name: "알파 편집" }));
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
    fireEvent.click(screen.getByRole("button", { name: "알파 편집" }));
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
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  expect(
    screen.getByRole("article", { name: "기록 원래 기록 · 이력" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("article", { name: "기록 철회 기록 · 이력" }),
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
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  expect(
    [...document.querySelectorAll(".lrow[data-id]")].map((n) =>
      n.getAttribute("data-id"),
    ),
  ).toEqual(["two"]);
});

it("keeps dictionary rows compact and expands only one with Enter, click and Escape", async () => {
  vi.spyOn(editing, "dictionary").mockResolvedValue([
    entry,
    { ...entry, name: "베타", aliases: [] },
  ]);
  const user = userEvent.setup();
  render(<DictionaryScreen />);
  const first = await screen.findByRole("button", { name: "알파 편집" });
  expect(screen.queryByLabelText("알파 이름")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("베타 이름")).not.toBeInTheDocument();
  first.focus();
  await user.keyboard("{Enter}");
  expect(screen.getByLabelText("알파 이름")).toBeInTheDocument();
  const second = screen.getByRole("button", { name: "베타 편집" });
  await user.click(second);
  expect(screen.queryByLabelText("알파 이름")).not.toBeInTheDocument();
  expect(screen.getByLabelText("베타 이름")).toBeInTheDocument();
  screen.getByLabelText("베타 새 별칭").focus();
  await user.keyboard("{Escape}");
  expect(screen.queryByLabelText("베타 이름")).not.toBeInTheDocument();
  expect(second).toHaveFocus();
});

it("preserves the expanded row and its DOM when aliases change", async () => {
  vi.spyOn(editing, "dictionary")
    .mockResolvedValueOnce([entry])
    .mockResolvedValue([{ ...entry, aliases: ["Alpha", "새 별칭"] }]);
  vi.spyOn(editing, "alias").mockResolvedValue();
  render(<DictionaryScreen />);
  fireEvent.click(await screen.findByRole("button", { name: "알파 편집" }));
  const input = screen.getByLabelText("알파 새 별칭");
  fireEvent.change(input, { target: { value: "새 별칭" } });
  fireEvent.click(screen.getByRole("button", { name: "별칭 추가" }));
  await screen.findByRole("button", { name: "새 별칭 별칭 삭제" });
  expect(screen.getByLabelText("알파 새 별칭")).toBe(input);
  expect(input).toHaveValue("");
});

it("hides tag corrections in both default views and shows target history with time, delta and actor", async () => {
  vi.spyOn(editing, "dictionary").mockResolvedValue([entry]);
  const correction: Thread = {
    ...thread,
    thread_id: "correction",
    seq: 2,
    kind: "태그정정",
    content: "태그 수정",
    tags: [],
    correction: { target: "one", add: ["새 태그"], remove: ["이전 태그"] },
  };
  const data: FabricData = {
    threads: [thread, correction],
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
    screen.queryByRole("article", { name: "태그정정 태그 수정" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("article", { name: "기록 원래 기록" }));
  const history = screen.getByRole("list", { name: "이력" });
  expect(history).toHaveTextContent("11/16 12:00");
  expect(history).toHaveTextContent("+새 태그");
  expect(history).toHaveTextContent("−이전 태그");
  expect(history).toHaveTextContent("직접 작성");
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  expect(
    [...document.querySelectorAll(".lrow[data-id]")].map((n) =>
      n.getAttribute("data-id"),
    ),
  ).toEqual(["one"]);
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  expect(
    [...document.querySelectorAll(".lrow[data-id]")].map((n) =>
      n.getAttribute("data-id"),
    ),
  ).toEqual(["one", "correction"]);
});

it("uses one history toggle for corrections and retractions", () => {
  const correction: Thread = {
    ...thread,
    thread_id: "meta",
    kind: "태그정정",
    correction: { target: "one", add: [], remove: [] },
  };
  const undo = { ...thread, thread_id: "undo-meta", retracts: "meta" };
  const rows = [thread, correction, undo];
  expect(filteredThreads(rows, initialFilters).map((t) => t.thread_id)).toEqual(
    ["one"],
  );
  expect(
    filteredThreads(rows, { ...initialFilters, history: true }).map(
      (t) => t.thread_id,
    ),
  ).toEqual(["one", "meta", "undo-meta"]);
});


it("shows an archived project's state in the dictionary", async () => {
  vi.spyOn(editing, "dictionary").mockResolvedValue([{ ...entry, archived: true }]);
  render(<DictionaryScreen />);
  expect(await screen.findByText("프로젝트 · 보관")).toBeVisible();
  expect(screen.getByRole("button", { name: "알파 편집" })).toBeEnabled();
});
