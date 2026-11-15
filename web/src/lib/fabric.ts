export interface Fact {
  thread_id: string;
  occurred_at: string;
  kind: string;
  tags: string[];
  source_id: string;
}
export const tagKinds: Record<string, string> = {
  project: "프로젝트",
  person: "사람",
  organization: "조직",
  topic: "주제",
};
export function groupKeys(
  t: Fact,
  group: string,
  kinds: Record<string, string | null>,
  sourceLabel: (id: string) => string,
): string[] {
  if (tagKinds[group]) {
    const names = t.tags.filter((name) => kinds[name] === group);
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
