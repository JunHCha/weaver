import { TagEditingProvider, TagFeedback } from "./TagEditing";
import { useKeyboardNavigation } from "../hooks/keyboard";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { FabricData, Thread } from "../lib/api";
import { groupKeys, tagKinds, type Fact, type Lane } from "../lib/fabric";
import {
  adjacentHistory,
  createThreadView,
  type ThreadView,
  historyTarget,
  isHistory,
  isValueHistory,
} from "../lib/history";
import { kinds } from "../lib/presentation";
import { CardBoard } from "./CardBoard";
import { DetailPanel } from "./DetailPanel";
import { FabricToolbar, initialFilters, type Filters } from "./FabricToolbar";
import { FocusBar } from "./FocusBar";
import { LogTable } from "./LogTable";
export function filteredThreads(
  threads: Thread[],
  f: Filters,
  includeValueHistory = false,
  view: ThreadView = createThreadView(threads, []),
): Thread[] {
  const relation = view.relation,
    now = new Date(),
    week = new Date(now);
  week.setDate(now.getDate() - ((now.getDay() + 6) % 7));
  week.setHours(0, 0, 0, 0);
  const matching = threads.filter(
    (t) =>
      (includeValueHistory || !isValueHistory(t)) &&
      (f.history || (!isHistory(t) && !relation.retracted.has(t.thread_id))) &&
      (!f.kinds.length || f.kinds.includes(t.kind)) &&
      (!f.tags.length ||
        t.tags.some((tag) => f.tags.includes(tag)) ||
        f.tags.includes(t.counterpart || "")) &&
      (f.period !== "week" || Date.parse(t.occurred_at) >= week.getTime()) &&
      (f.period !== "7d" ||
        Date.parse(t.occurred_at) >= now.getTime() - 7 * 86400000) &&
      (!f.openOnly || relation.isOpen(t)) &&
      (!f.query ||
        `${t.content || ""} ${t.counterpart || ""} ${t.tags.join(" ")} ${t.thread_id}`
          .toLowerCase()
          .includes(f.query.toLowerCase())),
  );
  if (!f.history) return matching;
  const ids = new Set(matching.map((t) => t.thread_id));
  const queue = [...ids];
  // Each snapshot ID enters the queue at most once, including cyclic metadata.
  const enqueue = (id: string) => {
    if (view.byId.has(id) && !ids.has(id)) {
      ids.add(id);
      queue.push(id);
    }
  };
  for (let i = 0; i < queue.length; i++) {
    const row = view.byId.get(queue[i])!;
    if (isHistory(row)) {
      const target = historyTarget(row);
      if (target) enqueue(target);
    }
    for (const child of view.children.get(row.thread_id) || [])
      enqueue(child.thread_id);
  }
  return adjacentHistory(
    threads.filter(
      (t) =>
        (includeValueHistory || !isValueHistory(t)) &&
        (ids.has(t.thread_id) ||
          (isHistory(t) && ids.has(historyTarget(t) || ""))),
    ),
  );
}
function RefreshAge({ at }: { at: number | null }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return at === null ? null : (
    <span className="mut refresh-age">
      {Math.max(0, Math.floor((now - at) / 1000))}초 전 갱신
    </span>
  );
}
function FabricContent({
  data,
  refreshedAt = null,
  onSaved,
}: {
  data: FabricData;
  onSaved?: () => void;
  onBusy?: (busy: boolean) => void;
  refreshedAt?: number | null;
}) {
  const [filters, setFilters] = useState<Filters>(initialFilters),
    [selected, setSelected] = useState<string | null>(null),
    [focusId, setFocus] = useState<string | null>(null),
    [panel, setPanel] = useState(false);
  const view = useMemo(
    () => createThreadView(data.threads, data.tags),
    [data.threads, data.tags],
  );
  const rows = useMemo(
    () => filteredThreads(data.threads, filters, false, view),
    [data.threads, filters, view],
  );
  const fieldRows = useMemo(
    () =>
      filteredThreads(
        data.threads,
        { ...filters, history: true },
        true,
        view,
      ).filter(isValueHistory),
    [data.threads, filters, view],
  );
  const changes = useMemo(() => {
    const ids = new Set(fieldRows.map((t) => t.thread_id));
    return view.changes.filter(({ thread }) => ids.has(thread.thread_id));
  }, [fieldRows, view]);
  const focus =
    filters.view === "card" && rows.some((t) => t.thread_id === focusId)
      ? focusId
      : null;
  const tagKindsByName = useMemo(
    () => Object.fromEntries(data.tags.map((t) => [t.name, t.kind])),
    [data.tags],
  );
  const sourcesById = useMemo(
    () => new Map(data.sources.map((source) => [source.id, source])),
    [data.sources],
  );
  const sourceTimes = useMemo(() => {
    const result = new Map<string, string>();
    data.threads.forEach((thread) => {
      if (!result.has(thread.source_id))
        result.set(thread.source_id, thread.occurred_at);
    });
    return result;
  }, [data.threads]);
  const sourceLabel = useCallback(
    (id: string) => {
      const source = sourcesById.get(id);
      return `${source?.kind || "출처"} ${(source?.updated_at || sourceTimes.get(id) || "").slice(5, 10).replace("-", "/")}`;
    },
    [sourcesById, sourceTimes],
  );
  const hiddenProjects = useMemo(
    () =>
      new Set(
        data.tags
          .filter(
            (tag) =>
              tag.kind === "project" && tag.archived && !filters.archived,
          )
          .map((tag) => tag.name),
      ),
    [data.tags, filters.archived],
  );
  const groups = useCallback(
    (t: Fact) =>
      groupKeys(
        view.targets.get(t.thread_id) || t,
        filters.group,
        tagKindsByName,
        sourceLabel,
        hiddenProjects,
      ),
    [view, filters.group, tagKindsByName, sourceLabel, hiddenProjects],
  );
  const lanes = useMemo<Lane[]>(() => {
    const counts = new Map<string, number>();
    rows.forEach((t) =>
      groups(t).forEach((key) => counts.set(key, (counts.get(key) || 0) + 1)),
    );
    const keys = [...counts.keys()],
      none =
        filters.group === "person"
          ? "상대 없음"
          : tagKinds[filters.group] + " 없음";
    if (tagKinds[filters.group])
      keys.sort(
        (a, b) =>
          Number(a === none) - Number(b === none) ||
          (counts.get(b) || 0) - (counts.get(a) || 0) ||
          a.localeCompare(b, "ko"),
      );
    if (filters.group === "kind")
      keys.sort(
        (a, b) =>
          kinds.findIndex((k) => k === a) - kinds.findIndex((k) => k === b),
      );
    return keys.map((key) => ({
      key,
      count: counts.get(key) || 0,
      none: !!tagKinds[filters.group] && key === none,
    }));
  }, [rows, groups, filters.group]);
  const pick = useCallback(
    (id: string, enterFocus: boolean) => {
      const target = view.byId.get(id);
      if (!target) return;
      // History lives outside row filters. Opening it must not change the view.
      if (!isValueHistory(target) && !rows.some((t) => t.thread_id === id)) {
        const archived = (view.targets.get(id) || target).tags.some((name) =>
          hiddenProjects.has(name),
        );
        if (isHistory(target) || archived)
          setFilters((old) => ({
            ...old,
            history: old.history || isHistory(target),
            archived: old.archived || archived,
          }));
      }
      setSelected(id);
      setPanel(true);
      if (enterFocus && filters.view === "card" && !isValueHistory(target))
        setFocus(id);
    },
    [rows, view, hiddenProjects, filters.view],
  );
  useKeyboardNavigation({
    rows,
    selected,
    focus,
    panel,
    setFocus,
    setPanel,
    setSelected,
  });
  const thread = selected ? view.byId.get(selected) || null : null;
  const pickCard = useCallback((id: string) => pick(id, true), [pick]);
  const selectCard = useCallback(
    (id: string) => pick(id, !!focus),
    [pick, focus],
  );
  const pickLog = useCallback((id: string) => pick(id, false), [pick]);
  return (
    <section className="screen">
      <div
        style={{ display: "flex", flex: 1, minHeight: 0, position: "relative" }}
      >
        <div className={"fabric-main" + (panel ? " pnl" : "")}>
          <div className="pagehead">
            <h1>Fabric</h1>
            <span className="count" id="cnt">
              Thread <b>{rows.length}</b>개 <RefreshAge at={refreshedAt} />
            </span>
          </div>
          <FabricToolbar
            filters={filters}
            tags={data.tags}
            onChange={(value) => {
              setFilters(value);
              if (value.view === "log") setFocus(null);
            }}
          />
          <TagFeedback />
          <FocusBar
            thread={focus ? view.byId.get(focus) || null : null}
            previous={Math.min(
              4,
              Math.max(
                0,
                rows.findIndex((t) => t.thread_id === focus),
              ),
            )}
            onExit={() => setFocus(null)}
          />
          {!lanes.length && !(filters.view === "log" && changes.length) ? (
            <div className="scroller" id="scr">
              <div className="empty">
                조건에 맞는 Thread가 없습니다. 필터를 줄여 보세요.
                <button
                  type="button"
                  onClick={() => {
                    setFilters({
                      ...initialFilters,
                      view: filters.view,
                      group: filters.group,
                    });
                    setFocus(null);
                  }}
                >
                  필터 초기화
                </button>
              </div>
            </div>
          ) : filters.view === "card" ? (
            <CardBoard
              states={data.states}
              changes={changes}
              view={view}
              selectedTags={filters.tags}
              rows={rows}
              lanes={lanes}
              groups={groups}
              group={filters.group}
              focus={focus}
              selected={selected}
              onPick={pickCard}
              onSelect={selectCard}
            />
          ) : (
            <LogTable
              changes={changes}
              view={view}
              selectedTags={filters.tags}
              rows={rows}
              lanes={lanes}
              groups={groups}
              group={filters.group}
              states={data.states}
              selected={selected}
              onPick={pickLog}
            />
          )}
        </div>
        <DetailPanel
          view={view}
          thread={panel ? thread : null}
          threads={data.threads}
          tags={data.tags}
          sources={data.sources}
          onSaved={onSaved}
          onClose={() => setPanel(false)}
          onPick={(id) => pick(id, !!focus)}
        />
      </div>
    </section>
  );
}

export function FabricScreen(props: Parameters<typeof FabricContent>[0]) {
  return (
    <TagEditingProvider onSaved={props.onSaved} onBusy={props.onBusy}>
      <FabricContent {...props} />
    </TagEditingProvider>
  );
}
