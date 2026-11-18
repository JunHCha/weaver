export interface Fact {
  thread_id: string;
  occurred_at: string;
  kind: string;
  tags: string[];
  source_id: string;
  counterpart?: string | null;
}
export const tagKinds: Record<string, string> = {
  project: "프로젝트",
  area: "영역",
  person: "사람",
  organization: "조직",
  topic: "주제",
};
export function groupKeys(
  t: Fact,
  group: string,
  kinds: Record<string, string | null>,
  sourceLabel: (id: string) => string,
  hiddenTags: ReadonlySet<string> = new Set(),
): string[] {
  if (group === "person") return [t.counterpart || "상대 없음"];
  if (tagKinds[group]) {
    const names = t.tags.filter(
      (name) => kinds[name] === group && !hiddenTags.has(name),
    );
    return names.length ? names : [`${tagKinds[group]} 없음`];
  }
  return [
    group === "kind"
      ? t.kind
      : group === "source"
        ? sourceLabel(t.source_id)
        : "",
  ];
}
export function selectFocus(
  rows: Fact[],
  id: string | null,
): Set<string> | null {
  if (!id) return null;
  const index = rows.findIndex((t) => t.thread_id === id);
  return index < 0
    ? null
    : new Set(rows.slice(Math.max(0, index - 4)).map((t) => t.thread_id));
}
export function packColumns(
  items: { lanes: number[]; heights: number[] }[],
  count: number,
  gap = 10,
) {
  const bottoms = Array<number>(count).fill(4);
  const positions = items.map((item) =>
    item.lanes.map((lane, copy) => {
      const top = bottoms[lane];
      bottoms[lane] += item.heights[copy] + gap;
      return top;
    }),
  );
  return { positions, bottoms };
}
export interface Point {
  x: number;
  y: number;
  lane: number;
}
export function weftPath(a: Point, b: Point): string {
  if (a.lane === b.lane) return `M${a.x - 6} ${a.y}L${b.x - 6} ${b.y}`;
  const d = Math.sign(b.x - a.x) || 1;
  return `M${a.x} ${a.y}C${a.x + d * 16} ${a.y} ${b.x - d * 16} ${b.y} ${b.x} ${b.y}`;
}
export function gapLabel(a: string, b: string): string {
  const m = Math.abs(Date.parse(a) - Date.parse(b)) / 60000;
  if (m < 1) return "같은 시각";
  if (m < 60) return `${Math.round(m)}분 뒤`;
  if (m < 1440) return `${Math.round(m / 60)}시간 뒤`;
  return `${Math.round(m / 1440)}일 뒤`;
}
export interface TagState {
  deadline: string | null;
  schedule: { start: string; end: string | null; timezone: string } | null;
  status: string | null;
  assignees: string[];
  open_requests: string[];
}
export function stateSummary(state: TagState): [string, string][] {
  const parts: [string, string][] = [];
  if (state.deadline)
    parts.push(["마감", state.deadline.slice(5).replace("-", "/")]);
  if (state.schedule)
    parts.push([
      "일정",
      `${state.schedule.start.slice(5, 10).replace("-", "/")} ${state.schedule.start.slice(11, 16)}–${state.schedule.end ? state.schedule.end.slice(11, 16) : "—"}`,
    ]);
  if (state.status) parts.push(["상태", state.status]);
  if (state.assignees.length)
    parts.push(["담당자", state.assignees.join(", ")]);
  parts.push(["열린 요청", String(state.open_requests.length)]);
  return parts;
}

/** Fit at most thirty hops into six seconds while preserving the 200ms floor. */
export function pulseDurations(lengths: number[]): number[] {
  const durations = lengths
    .slice(0, 30)
    .map((l) => 350 + 350 * Math.min(1, l / 900));
  const sum = durations.reduce((a, b) => a + b, 0);
  if (sum <= 6000) return durations;
  const floorBudget = durations.length * 200;
  const scale = (6000 - floorBudget) / (sum - floorBudget);
  return durations.map((ms) => 200 + (ms - 200) * scale);
}

export function highlightedSegments(
  order: string[],
  id: string | null,
): Set<number> {
  const index = id ? order.indexOf(id) : -1;
  return new Set(
    index < 0
      ? []
      : order
          .slice(0, -1)
          .flatMap((_, i) => (i >= index - 2 && i <= index + 1 ? [i] : [])),
  );
}
export interface Lane {
  key: string;
  count: number;
  none: boolean;
}
export interface PlacedCard {
  key: string;
  id: string;
  lane: number;
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface BoardLayout {
  lanes: (Lane & {
    x: number;
    width: number;
    collapsed: boolean;
    visible: number;
  })[];
  cards: PlacedCard[];
  width: number;
  height: number;
}
export function focusLayout(
  rows: Fact[],
  lanes: Lane[],
  groups: (t: Fact) => string[],
  focus: string | null,
  collapsed: boolean,
  viewport: number,
  heights: Record<string, number>,
  single: boolean,
): BoardLayout {
  const selected = selectFocus(rows, focus);
  const visible = rows.filter((t) => !selected || selected.has(t.thread_id));
  const laneIndex = new Map(lanes.map((lane, i) => [lane.key, i]));
  const memberships = visible.map((t) => ({
    t,
    lanes: groups(t).flatMap((key) => {
      const i = laneIndex.get(key);
      return i === undefined ? [] : [i];
    }),
  }));
  const counts = Array<number>(lanes.length).fill(0);
  memberships.forEach(({ lanes }) => lanes.forEach((i) => counts[i]++));
  const strips = lanes.map((l, i) => (l.none && collapsed) || counts[i] === 0);
  const closed = strips.filter(Boolean).length;
  const width = single
    ? Math.min(720, viewport - 96)
    : Math.max(
        190,
        Math.min(
          300,
          Math.floor(
            (viewport - 48 - (lanes.length - 1) * 56 - closed * 44) /
              Math.max(1, lanes.length - closed),
          ),
        ),
      );
  let x = single ? Math.max(48, Math.floor((viewport - width) / 2)) : 24;
  const placedLanes = lanes.map((l, i) => {
    const lane = {
      ...l,
      x,
      width: strips[i] ? 44 : width,
      collapsed: strips[i],
      visible: counts[i],
    };
    x += lane.width + (single ? 0 : 56);
    return lane;
  });
  const items = memberships.map(({ t, lanes }) => ({
    t,
    lanes: lanes.filter((i) => !strips[i]).sort((a, b) => a - b),
  }));
  const packed = packColumns(
    items.map(({ t, lanes }) => ({
      lanes,
      heights: lanes.map((i) => heights[`${t.thread_id}:${i}`] || 100),
    })),
    lanes.length,
  );
  const cards = items.flatMap(({ t, lanes }, index) =>
    lanes.map((lane, copy) => ({
      key: `${t.thread_id}:${lane}`,
      id: t.thread_id,
      lane,
      x: placedLanes[lane].x,
      y: packed.positions[index][copy],
      width: placedLanes[lane].width,
      height: heights[`${t.thread_id}:${lane}`] || 100,
    })),
  );
  return {
    lanes: placedLanes,
    cards,
    width: Math.max(viewport, x - (single ? 0 : 56) + 24),
    height: Math.max(4, ...packed.bottoms) + 60,
  };
}
