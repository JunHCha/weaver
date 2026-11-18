import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { editing, type DictionaryEntry, type BatchTagResult } from "../lib/api";
import { fixture, thread } from "../test/fabricFixture";
import { FabricScreen } from "./FabricScreen";
import { DictionaryScreen } from "./DictionaryScreen";
import { TagEditor } from "./TagEditor";
const entries: DictionaryEntry[] = fixture.tags.map((tag) => ({
  ...tag,
  kind: tag.kind!,
  aliases: tag.name === "B" ? ["Beta"] : [],
  human_edited: true,
  usage_count: 1,
}));
const data = {
  ...fixture,
  threads: [thread("one"), thread("two", { tags: ["B"] })],
};
const result = (ids: string[]): BatchTagResult => ({
  corrections: ids.map((id) =>
    thread("correction-" + id, {
      kind: "태그정정",
      correction: { target: id, add: ["B"], remove: [] },
    }),
  ),
  changed: ids.length,
  skipped: 0,
  skipped_threads: [],
});
beforeEach(() => {
  vi.spyOn(editing, "dictionary").mockResolvedValue(entries);
  vi.spyOn(editing, "dismissed").mockResolvedValue([]);
  vi.spyOn(editing, "tags").mockImplementation(
    async (id) => result([id]).corrections[0],
  );
  vi.spyOn(editing, "batchTags").mockImplementation(async (ids) => result(ids));
  vi.spyOn(editing, "undoTags").mockResolvedValue();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});
async function openCard() {
  const card = screen.getByRole("article", { name: "기록 합성 one" });
  fireEvent.click(
    within(card).getByRole("button", { name: "추가할 태그 열기" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("추가할 태그")).toBeEnabled(),
  );
  return card;
}
it("edits card chips, exposes kind text and archived labels, uses alias autocomplete and returns focus", async () => {
  vi.mocked(editing.dictionary).mockResolvedValue(
    entries.map((entry) => ({ ...entry, archived: entry.name === "A" })),
  );
  render(<FabricScreen data={data} />);
  const card = await openCard();
  expect(within(card).getByText("보관")).toBeVisible();
  expect(within(card).getByText("사람")).toBeVisible();
  const input = screen.getByRole("combobox", { name: "추가할 태그" });
  fireEvent.change(input, { target: { value: "Beta" } });
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).toHaveValue("B");
  expect(editing.tags).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "추가" }));
  await waitFor(() =>
    expect(editing.tags).toHaveBeenCalledWith("one", {
      add: ["B"],
      remove: [],
      kinds: {},
    }),
  );
  expect(
    within(card).getByRole("button", { name: "추가할 태그 열기" }),
  ).toHaveFocus();
  expect(
    within(card).getByRole("button", { name: "one 태그 되돌리기" }),
  ).toBeVisible();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("uses one atomic batch call, reports skipped rows and removes unknown tag names", async () => {
  vi.mocked(editing.batchTags).mockResolvedValue({
    ...result(["one"]),
    skipped: 1,
    skipped_threads: [
      { thread_id: "two", reason: "이미 같은 태그 상태입니다." },
    ],
  });
  render(<FabricScreen data={data} />);
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  fireEvent.click(screen.getByLabelText("모든 Thread 선택"));
  const bulk = screen.getByRole("region", { name: "태그 일괄 편집" });
  expect(bulk).toHaveTextContent("선택한 2개에");
  await waitFor(() =>
    expect(within(bulk).getByLabelText("추가할 태그")).toBeEnabled(),
  );
  fireEvent.change(within(bulk).getByLabelText("추가할 태그"), {
    target: { value: "Beta" },
  });
  fireEvent.click(within(bulk).getByRole("button", { name: "일괄 추가" }));
  await waitFor(() =>
    expect(editing.batchTags).toHaveBeenCalledWith(["one", "two"], {
      add: ["B"],
      remove: [],
      kinds: {},
    }),
  );
  expect(editing.tags).not.toHaveBeenCalled();
  expect(
    screen.getByRole("status", { name: "태그 편집 결과" }),
  ).toHaveTextContent("1개 변경 · 1개 건너뜀two: 이미 같은 태그 상태입니다.");
  fireEvent.change(within(bulk).getByLabelText("추가할 태그"), {
    target: { value: "사전에 없는 태그" },
  });
  fireEvent.click(within(bulk).getByRole("button", { name: "일괄 삭제" }));
  await waitFor(() =>
    expect(editing.batchTags).toHaveBeenLastCalledWith(["one", "two"], {
      add: [],
      remove: ["사전에 없는 태그"],
    }),
  );
});
it("reports a no-op batch and does not offer undo", async () => {
  vi.mocked(editing.batchTags).mockResolvedValue({
    corrections: [],
    changed: 0,
    skipped: 2,
    skipped_threads: [
      { thread_id: "one", reason: "같음" },
      { thread_id: "two", reason: "같음" },
    ],
  });
  render(<TagEditor thread={data.threads[0]} threads={data.threads} bulk />);
  await waitFor(() =>
    expect(screen.getByLabelText("추가할 태그")).toBeEnabled(),
  );
  fireEvent.change(screen.getByLabelText("추가할 태그"), {
    target: { value: "B" },
  });
  fireEvent.click(screen.getByRole("button", { name: "일괄 추가" }));
  expect(
    await screen.findByRole("status", { name: "태그 편집 결과" }),
  ).toHaveTextContent("변경된 태그가 없습니다.");
  expect(
    screen.queryByRole("button", { name: "되돌리기" }),
  ).not.toBeInTheDocument();
});
it("requires a kind for creation, refreshes first and names a newly conflicting entry", async () => {
  const { rerender } = render(<TagEditor thread={data.threads[0]} />);
  await waitFor(() =>
    expect(screen.getByLabelText("추가할 태그")).toBeEnabled(),
  );
  const input = screen.getByLabelText("추가할 태그");
  fireEvent.change(input, { target: { value: "새 영역" } });
  expect(screen.getByRole("button", { name: "추가" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("새 태그 종류"), {
    target: { value: "area" },
  });
  vi.mocked(editing.dictionary).mockResolvedValue([
    ...entries,
    { ...entries[0], name: "새 영역", kind: "person" },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "추가" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "이미 등록된 항목: 새 영역",
  );
  expect(editing.tags).not.toHaveBeenCalled();
  rerender(<TagEditor thread={data.threads[0]} />);
  fireEvent.change(input, { target: { value: "다른 새 영역" } });
  fireEvent.change(screen.getByLabelText("새 태그 종류"), {
    target: { value: "area" },
  });
  fireEvent.click(screen.getByRole("button", { name: "추가" }));
  await waitFor(() =>
    expect(editing.tags).toHaveBeenCalledWith("one", {
      add: ["다른 새 영역"],
      remove: [],
      kinds: { "다른 새 영역": "area" },
    }),
  );
});
it("does not submit during Korean IME composition", async () => {
  render(<FabricScreen data={data} />);
  await openCard();
  const input = screen.getByLabelText("추가할 태그");
  fireEvent.change(input, { target: { value: "Beta" } });
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 });
  fireEvent.submit(input.closest("form")!);
  expect(editing.tags).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.click(screen.getByRole("button", { name: "추가" }));
  await waitFor(() => expect(editing.tags).toHaveBeenCalledTimes(1));
});
it("keeps optimistic chips during a poll, repositions on scroll/resize and closes when the card disappears", async () => {
  let finish!: (value: ReturnType<typeof thread>) => void;
  vi.mocked(editing.tags).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const { rerender } = render(<FabricScreen data={data} />);
  const card = await openCard();
  expect(screen.getByRole("dialog")).toHaveAccessibleName("one 태그 추가");
  const anchor = within(card).getByRole("button", { name: "추가할 태그 열기" });
  const popup = screen.getByRole("dialog");
  let top = 200;
  vi.spyOn(anchor, "getBoundingClientRect").mockImplementation(() => ({
    left: 100,
    top,
    bottom: top + 24,
    right: 124,
    width: 24,
    height: 24,
    x: 100,
    y: top,
    toJSON: () => ({}),
  }));
  vi.spyOn(card, "getBoundingClientRect").mockReturnValue({
    right: window.innerWidth,
  } as DOMRect);
  vi.spyOn(popup, "getBoundingClientRect").mockReturnValue({
    height: 80,
  } as DOMRect);
  fireEvent.scroll(document);
  expect(popup).toHaveStyle({ top: "116px", left: "100px" });
  top = 400;
  fireEvent.resize(window);
  expect(popup).toHaveStyle({ top: "316px" });
  fireEvent.click(within(card).getByRole("button", { name: "A 태그 삭제" }));
  rerender(
    <FabricScreen
      data={{
        ...data,
        threads: [
          { ...data.threads[0], tags: [...data.threads[0].tags] },
          data.threads[1],
        ],
      }}
    />,
  );
  expect(
    within(card).queryByRole("button", { name: "A 태그 삭제" }),
  ).not.toBeInTheDocument();
  await act(async () => finish(result(["one"]).corrections[0]));
  rerender(<FabricScreen data={{ ...data, threads: [] }} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("undoes correction IDs after a card disappears and expires after five seconds", async () => {
  const { rerender } = render(<FabricScreen data={data} />);
  fireEvent.click(
    within(screen.getByRole("article", { name: "기록 합성 one" })).getByRole(
      "button",
      { name: "A 태그 삭제" },
    ),
  );
  const undo = await screen.findByRole("button", { name: "되돌리기" });
  rerender(<FabricScreen data={{ ...data, threads: [] }} />);
  fireEvent.click(undo);
  await waitFor(() =>
    expect(editing.undoTags).toHaveBeenCalledWith("correction-one"),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "되돌리기" }),
    ).not.toBeInTheDocument(),
  );
  rerender(<FabricScreen data={data} />);
  vi.useFakeTimers();
  await act(async () =>
    fireEvent.click(
      within(screen.getByRole("article", { name: "기록 합성 one" })).getByRole(
        "button",
        { name: "A 태그 삭제" },
      ),
    ),
  );
  act(() => vi.advanceTimersByTime(4999));
  expect(screen.getByRole("button", { name: "되돌리기" })).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(1));
  expect(
    screen.queryByRole("button", { name: "되돌리기" }),
  ).not.toBeInTheDocument();
});
it("excludes every history class from editing and bulk selection", async () => {
  const histories = [
    thread("correction", {
      kind: "태그정정",
      correction: { target: "one", add: [], remove: [] },
    }),
    thread("reject", { kind: "거절", content: null }),
    thread("retract", { retracts: "two" }),
    thread("wiki-field:status", { field: "status", value: "완료" }),
    thread("exec", { kind: "실행", executes: "one" }),
  ];
  render(
    <FabricScreen
      data={{ ...data, threads: [...data.threads, ...histories] }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "이력 포함" }));
  for (const item of histories) {
    const row = document.querySelector<HTMLElement>(
      `.card[data-id="${item.thread_id}"]`,
    );
    if (row)
      expect(within(row).queryByLabelText("태그 편집")).not.toBeInTheDocument();
  }
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  for (const row of histories)
    expect(
      screen.queryByLabelText(`${row.thread_id} Thread 선택`),
    ).not.toBeInTheDocument();
  fireEvent.click(screen.getByLabelText("모든 Thread 선택"));
  expect(
    screen.getByRole("region", { name: "태그 일괄 편집" }),
  ).toHaveTextContent("선택한 2개에");
});
it("confirms a suggestion, persists dismissals and shows years in last used dates", async () => {
  const similar = [
    {
      ...entries[0],
      name: "프로젝트 알파",
      last_used_at: "2026-11-18T09:00:00+09:00",
      usage_count: 4,
    },
    { ...entries[0], name: "프로젝트 알퍼" },
  ];
  vi.mocked(editing.dictionary).mockResolvedValue(similar);
  const merge = vi.spyOn(editing, "merge").mockResolvedValue(),
    dismiss = vi.spyOn(editing, "dismiss").mockResolvedValue();
  render(<DictionaryScreen />);
  expect(await screen.findByText("최근 2026/11/18 09:00")).toBeVisible();
  const suggestion = screen.getByRole("region", {
    name: "프로젝트 알퍼 프로젝트 알파 합치기 제안",
  });
  fireEvent.click(within(suggestion).getByRole("button", { name: "합치기" }));
  expect(merge).not.toHaveBeenCalled();
  const dialog = screen.getByRole("dialog", { name: "합치기 확인" });
  expect(dialog).toHaveTextContent(
    "프로젝트 알파 이름이 남고 프로젝트 알퍼은 별칭이 됩니다.",
  );
  expect(dialog).toHaveTextContent("합친 뒤 종류: 프로젝트");
  fireEvent.click(within(dialog).getByRole("button", { name: "합치기 확인" }));
  await waitFor(() =>
    expect(merge).toHaveBeenCalledWith(
      "프로젝트 알퍼",
      "프로젝트 알파",
      undefined,
    ),
  );
  await waitFor(() =>
    expect(
      within(suggestion).getByRole("button", { name: "무시" }),
    ).toBeEnabled(),
  );
  fireEvent.click(within(suggestion).getByRole("button", { name: "무시" }));
  await waitFor(() =>
    expect(dismiss).toHaveBeenCalledWith("프로젝트 알퍼", "프로젝트 알파"),
  );
});
it("requires an explicit resulting kind for a manual cross-kind merge", async () => {
  vi.spyOn(editing, "merge").mockResolvedValue();
  render(<DictionaryScreen />);
  fireEvent.click(await screen.findByRole("button", { name: "A 편집" }));
  fireEvent.change(screen.getByLabelText("A 합칠 대상"), {
    target: { value: "민수" },
  });
  fireEvent.click(screen.getByRole("button", { name: "합치기" }));
  const dialog = screen.getByRole("dialog");
  expect(
    within(dialog).getByRole("button", { name: "합치기 확인" }),
  ).toBeDisabled();
  fireEvent.change(within(dialog).getByLabelText("합친 뒤 종류"), {
    target: { value: "area" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "합치기 확인" }));
  await waitFor(() =>
    expect(editing.merge).toHaveBeenCalledWith("A", "민수", "area"),
  );
});

it("closes an open popup when polling removes its card", async () => {
  const { rerender } = render(<FabricScreen data={data} />);
  await openCard();
  expect(screen.getByRole("dialog", { name: "one 태그 추가" })).toBeVisible();
  rerender(<FabricScreen data={{ ...data, threads: [data.threads[1]] }} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("waits for persisted dismissals before showing suggestions after reload", async () => {
  vi.mocked(editing.dictionary).mockResolvedValue([
    { ...entries[0], name: "프로젝트알파" },
    { ...entries[0], name: "프로젝트알퍼" },
  ]);
  let finish!: (pairs: [string, string][]) => void;
  vi.mocked(editing.dismissed).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(<DictionaryScreen />);
  await screen.findByRole("button", { name: "프로젝트알파 편집" });
  expect(
    screen.queryByRole("region", { name: "비슷한 태그 묶음" }),
  ).not.toBeInTheDocument();
  await act(async () => finish([["프로젝트알파", "프로젝트알퍼"]]));
  expect(
    screen.queryByRole("region", { name: "비슷한 태그 묶음" }),
  ).not.toBeInTheDocument();
});
