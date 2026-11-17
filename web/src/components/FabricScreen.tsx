import { useKeyboardNavigation } from "../hooks/keyboard";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { FabricData, Thread } from "../lib/api";
import { groupKeys, tagKinds, type Fact, type Lane } from "../lib/fabric";
import { kinds, relations } from "../lib/presentation";
import { CardBoard } from "./CardBoard";
import { DetailPanel } from "./DetailPanel";
import { FabricToolbar, initialFilters, type Filters } from "./FabricToolbar";
import { FocusBar } from "./FocusBar";
import { LogTable } from "./LogTable";
export function filteredThreads(threads: Thread[], f: Filters): Thread[] {
  const relation = relations(threads),
    now = new Date(),
    week = new Date(now);
  week.setDate(now.getDate() - ((now.getDay() + 6) % 7));
  week.setHours(0, 0, 0, 0);
  const metadata = new Set(
    threads
      .filter((t) => ["실행", "태그정정"].includes(t.kind))
      .map((t) => t.thread_id),
  );
  const visible = (t: Thread) =>
    f.executions ||
    (!metadata.has(t.thread_id) && !metadata.has(t.retracts || ""));
  const matching = threads.filter(
    (t) =>
      (f.retractions ||
        (!t.retracts && !relation.retracted.has(t.thread_id))) &&
      visible(t) &&
      (!f.kinds.length || f.kinds.includes(t.kind)) &&
      (!f.tags.length || t.tags.some((tag) => f.tags.includes(tag))) &&
      (f.period !== "week" || Date.parse(t.occurred_at) >= week.getTime()) &&
      (f.period !== "7d" ||
        Date.parse(t.occurred_at) >= now.getTime() - 7 * 86400000) &&
      (!f.openOnly || relation.isOpen(t)) &&
      (!f.query ||
        `${t.content || ""} ${t.tags.join(" ")} ${t.thread_id}`
          .toLowerCase()
          .includes(f.query.toLowerCase())),
  );
  if (!f.retractions) return matching;
  const ids = new Set(matching.map((t) => t.thread_id));
  const targets = new Set(
    matching.flatMap((t) => (t.retracts ? [t.retracts] : [])),
  );
  return threads.filter(
    (t) =>
      visible(t) &&
      (ids.has(t.thread_id) ||
        targets.has(t.thread_id) ||
        (!!t.retracts && ids.has(t.retracts))),
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
export function FabricScreen({
  data,
  refreshedAt = null,
  onSaved,
}: {
  data: FabricData;
  onSaved?: () => void;
  refreshedAt?: number | null;
}) {
  const [filters, setFilters] = useState<Filters>(initialFilters),
    [selected, setSelected] = useState<string | null>(null),
    [focusId, setFocus] = useState<string | null>(null),
    [panel, setPanel] = useState(false);
  const rows = useMemo(
    () => filteredThreads(data.threads, filters),
    [data.threads, filters],
  );
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
  const threadsById = useMemo(
    () => new Map(data.threads.map((t) => [t.thread_id, t])),
    [data.threads],
  );
  const hiddenProjects = useMemo(
    () =>
      new Set(
        data.tags
          .filter((tag) => tag.kind === "project" && tag.archived && !filters.archived)
          .map((tag) => tag.name),
      ),
    [data.tags, filters.archived],
  );
  const groups = useCallback(
    (t: Fact) => {
      const row = threadsById.get(t.thread_id);
      const target = row?.retracts ? threadsById.get(row.retracts) : undefined;
      return groupKeys(
        target || t, filters.group, tagKindsByName, sourceLabel, hiddenProjects,
      );
    },
    [filters.group, tagKindsByName, sourceLabel, threadsById, hiddenProjects],
  );
  const lanes = useMemo<Lane[]>(() => {
    const counts = new Map<string, number>();
    rows.forEach((t) =>
      groups(t).forEach((key) => counts.set(key, (counts.get(key) || 0) + 1)),
    );
    const keys = [...counts.keys()],
      none = tagKinds[filters.group] + " 없음";
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
      if (!rows.some((t) => t.thread_id === id))
        setFilters({
          ...initialFilters,
          view: filters.view,
          group: filters.group,
          executions: true,
          retractions: true,
        });
      setSelected(id);
      setPanel(true);
      if (enterFocus && filters.view === "card") setFocus(id);
    },
    [rows, filters.view, filters.group],
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
  const thread = data.threads.find((t) => t.thread_id === selected) || null;
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
          <FocusBar
            thread={
              focus
                ? data.threads.find((t) => t.thread_id === focus) || null
                : null
            }
            previous={Math.min(
              4,
              Math.max(
                0,
                rows.findIndex((t) => t.thread_id === focus),
              ),
            )}
            onExit={() => setFocus(null)}
          />
          {!rows.length ? (
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
              rows={rows}
              threads={data.threads}
              tags={data.tags}
              lanes={lanes}
              groups={groups}
              group={filters.group}
              focus={focus}
              selected={selected}
              onPick={(id) => pick(id, true)}
              onSelect={(id) => pick(id, !!focus)}
            />
          ) : (
            <LogTable
              rows={rows}
              threads={data.threads}
              tags={data.tags}
              lanes={lanes}
              groups={groups}
              group={filters.group}
              states={data.states}
              selected={selected}
              onPick={(id) => pick(id, false)}
            />
          )}
        </div>
        <DetailPanel
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
