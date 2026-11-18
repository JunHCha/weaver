import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { editing, type DictionaryEntry, type TagEdit } from "../lib/api";
import { fixture, thread } from "../test/fabricFixture";
import { FabricScreen } from "./FabricScreen";
import { DictionaryScreen } from "./DictionaryScreen";
import { TagEditor } from "./TagEditor";
import { tagSuggestions } from "../lib/tagSuggestions";
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
beforeEach(() => {
  vi.spyOn(editing, "dictionary").mockResolvedValue(entries);
  vi.spyOn(editing, "tags").mockImplementation(async (id, edit) =>
    thread("correction-" + id, {
      kind: "태그정정",
      correction: { target: id, add: edit.add, remove: edit.remove },
    }),
  );
  vi.spyOn(editing, "undoTags").mockResolvedValue();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("edits colored card chips directly and adds a dictionary alias without opening detail", async () => {
  render(<FabricScreen data={data} />);
  const card = screen.getByRole("article", { name: "기록 합성 one" });
  await waitFor(() =>
    expect(within(card).getByText("#민수")).toHaveAttribute(
      "data-kind",
      "person",
    ),
  );
  fireEvent.click(within(card).getByRole("button", { name: "민수 태그 삭제" }));
  await waitFor(() =>
    expect(editing.tags).toHaveBeenCalledWith("one", {
      add: [],
      remove: ["민수"],
    }),
  );
  fireEvent.click(
    within(card).getByRole("button", { name: "추가할 태그 열기" }),
  );
  fireEvent.change(screen.getByLabelText("추가할 태그"), {
    target: { value: "Beta" },
  });
  fireEvent.click(screen.getByRole("button", { name: "추가" }));
  await waitFor(() =>
    expect(editing.tags).toHaveBeenLastCalledWith("one", {
      add: ["B"],
      remove: [],
      kinds: {},
    }),
  );
  expect(
    screen.getByRole("complementary", { name: "Thread 상세" }),
  ).toBeEmptyDOMElement();
});

it("edits log row chips and bulk adds/removes one correction per selected Thread", async () => {
  render(<FabricScreen data={data} />);
  fireEvent.click(screen.getByRole("button", { name: "로그" }));
  const row = document.querySelector('.lrow[data-id="one"]') as HTMLElement;
  fireEvent.click(within(row).getByRole("button", { name: "A 태그 삭제" }));
  await waitFor(() => expect(editing.tags).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByLabelText("모든 Thread 선택"));
  const bulk = screen.getByRole("region", { name: "태그 일괄 편집" });
  fireEvent.change(within(bulk).getByLabelText("추가할 태그"), {
    target: { value: "Beta" },
  });
  fireEvent.click(within(bulk).getByRole("button", { name: "일괄 추가" }));
  await waitFor(() => expect(editing.tags).toHaveBeenCalledTimes(3));
  expect(editing.tags).toHaveBeenNthCalledWith(2, "one", {
    add: ["B"],
    remove: [],
    kinds: {},
  });
  expect(editing.tags).toHaveBeenNthCalledWith(3, "two", {
    add: ["B"],
    remove: [],
    kinds: {},
  });
  fireEvent.change(within(bulk).getByLabelText("추가할 태그"), {
    target: { value: "Beta" },
  });
  fireEvent.click(within(bulk).getByRole("button", { name: "일괄 삭제" }));
  await waitFor(() => expect(editing.tags).toHaveBeenCalledTimes(5));
  expect(editing.tags).toHaveBeenNthCalledWith(4, "one", {
    add: [],
    remove: ["B"],
  });
  expect(editing.tags).toHaveBeenNthCalledWith(5, "two", {
    add: [],
    remove: ["B"],
  });
});

it("creates an unknown entry inline only after choosing its kind", async () => {
  render(<FabricScreen data={data} />);
  fireEvent.click(
    within(screen.getByRole("article", { name: "기록 합성 one" })).getByRole(
      "button",
      { name: "추가할 태그 열기" },
    ),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("추가할 태그")).toBeEnabled(),
  );
  fireEvent.change(screen.getByLabelText("추가할 태그"), {
    target: { value: "새 영역" },
  });
  expect(screen.getByRole("button", { name: "추가" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("새 태그 종류"), {
    target: { value: "area" },
  });
  fireEvent.click(screen.getByRole("button", { name: "추가" }));
  await waitFor(() =>
    expect(editing.tags).toHaveBeenCalledWith("one", {
      add: ["새 영역"],
      remove: [],
      kinds: { "새 영역": "area" },
    }),
  );
});

it("undoes correction IDs after the edited card disappears and expires after five seconds", async () => {
  const onSaved = vi.fn();
  const { rerender } = render(<FabricScreen data={data} onSaved={onSaved} />);
  fireEvent.click(
    within(screen.getByRole("article", { name: "기록 합성 one" })).getByRole(
      "button",
      { name: "A 태그 삭제" },
    ),
  );
  const undo = await screen.findByRole("button", { name: "되돌리기" });
  rerender(<FabricScreen data={{ ...data, threads: [] }} onSaved={onSaved} />);
  fireEvent.click(undo);
  await waitFor(() =>
    expect(editing.undoTags).toHaveBeenCalledWith("correction-one"),
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "되돌리기" }),
    ).not.toBeInTheDocument(),
  );
  rerender(<FabricScreen data={data} onSaved={onSaved} />);
  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole("article", { name: "기록 합성 one" })).getByRole(
        "button",
        { name: "A 태그 삭제" },
      ),
    );
  });
  expect(screen.getByRole("button", { name: "되돌리기" })).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(4999));
  expect(screen.getByRole("button", { name: "되돌리기" })).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(1));
  expect(
    screen.queryByRole("button", { name: "되돌리기" }),
  ).not.toBeInTheDocument();
});

it("keeps successful bulk writes undoable when another write fails", async () => {
  vi.mocked(editing.tags).mockImplementation(async (id, edit: TagEdit) => {
    if (id === "two") throw new Error("일부 저장 실패");
    return thread("saved", {
      kind: "태그정정",
      correction: { target: id, ...edit },
    });
  });
  render(<TagEditor thread={data.threads[0]} threads={data.threads} bulk />);
  await waitFor(() =>
    expect(screen.getByLabelText("추가할 태그")).toBeEnabled(),
  );
  fireEvent.change(screen.getByLabelText("추가할 태그"), {
    target: { value: "B" },
  });
  fireEvent.click(screen.getByRole("button", { name: "일괄 추가" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("일부 저장 실패");
  fireEvent.click(screen.getByRole("button", { name: "되돌리기" }));
  await waitFor(() => expect(editing.undoTags).toHaveBeenCalledWith("saved"));
});

it("suggests spacing, close typos and alias overlaps and merges or retains the source as alias in one click", async () => {
  const entry = (name: string, aliases: string[] = []): DictionaryEntry => ({
    ...entries[0],
    name,
    aliases,
  });
  const similar = [
    entry("프로젝트 알파"),
    entry("프로젝트알파"),
    entry("프로젝트알퍼"),
    entry("별칭 항목", ["프로젝트 알파"]),
  ];
  expect(new Set(tagSuggestions(similar).map((pair) => pair.reason))).toEqual(
    new Set(["띄어쓰기", "비슷한 이름", "별칭 겹침"]),
  );
  vi.mocked(editing.dictionary).mockResolvedValue(similar);
  const merge = vi.spyOn(editing, "merge").mockResolvedValue();
  render(<DictionaryScreen />);
  const suggestion = await screen.findByRole("region", {
    name: "프로젝트알파 프로젝트 알파 합치기 제안",
  });
  fireEvent.click(within(suggestion).getByRole("button", { name: "합치기" }));
  await waitFor(() =>
    expect(merge).toHaveBeenCalledWith("프로젝트알파", "프로젝트 알파"),
  );
  await waitFor(() =>
    expect(
      within(suggestion).getByRole("button", { name: "별칭으로 넣기" }),
    ).toBeEnabled(),
  );
  fireEvent.click(
    within(suggestion).getByRole("button", { name: "별칭으로 넣기" }),
  );
  await waitFor(() => expect(merge).toHaveBeenCalledTimes(2));
});

it("undoes every bulk correction and retries only failed retractions", async () => {
  vi.mocked(editing.undoTags)
    .mockResolvedValueOnce()
    .mockRejectedValueOnce(new Error("되돌리기 실패"))
    .mockResolvedValue();
  render(<TagEditor thread={data.threads[0]} threads={data.threads} bulk />);
  await waitFor(() =>
    expect(screen.getByLabelText("추가할 태그")).toBeEnabled(),
  );
  fireEvent.change(screen.getByLabelText("추가할 태그"), {
    target: { value: "민수" },
  });
  fireEvent.click(screen.getByRole("button", { name: "일괄 추가" }));
  fireEvent.click(await screen.findByRole("button", { name: "되돌리기" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("되돌리기 실패");
  expect(editing.undoTags).toHaveBeenNthCalledWith(1, "correction-one");
  expect(editing.undoTags).toHaveBeenNthCalledWith(2, "correction-two");
  fireEvent.click(screen.getByRole("button", { name: "되돌리기" }));
  await waitFor(() => expect(editing.undoTags).toHaveBeenCalledTimes(3));
  expect(editing.undoTags).toHaveBeenLastCalledWith("correction-two");
});

it("shows dictionary usage counts and recent usage dates including unused entries", async () => {
  vi.mocked(editing.dictionary).mockResolvedValue([
    {
      ...entries[0],
      last_used_at: "2026-11-18T09:00:00+09:00",
      usage_count: 4,
    },
    { ...entries[1], last_used_at: null, usage_count: 0 },
  ]);
  render(<DictionaryScreen />);
  expect(await screen.findByText("최근 11/18 09:00")).toBeVisible();
  expect(screen.getByText("Thread 4개")).toBeVisible();
  expect(screen.getByText("최근 사용 없음")).toBeVisible();
});
