import type { CSSProperties } from "react";
import type { Thread, Kind, JsonValue } from "./api";
export const kinds: Kind[] = [
  "요청",
  "결정",
  "일정",
  "기록",
  "태그정정",
  "거절",
  "실행",
];
export const kindClass: Record<Kind, string> = {
  요청: "req",
  결정: "dec",
  일정: "sch",
  기록: "rec",
  태그정정: "hol",
  거절: "hol",
  실행: "hol",
};
export const actorLabel = {
  human: "직접 작성",
  ai_approved: "AI 승인",
  system: "시스템",
};
export const fieldLabel = {
  deadline: "마감",
  schedule: "일정",
  assignees: "담당자",
  status: "상태",
};
export const refs = [
  "resolves",
  "retracts",
  "updates",
  "amends",
  "blocked_by",
  "executes",
] as const;
export const refLabels = {
  resolves: "해결한 요청",
  retracts: "철회한 Thread",
  updates: "변경한 일정",
  amends: "기한을 변경한 요청",
  blocked_by: "선행 요청",
  executes: "실행 대상",
};
export function kindStyle(kind: Kind): CSSProperties {
  return { "--k": `var(--c-${kindClass[kind]})` } as CSSProperties;
}
export const shortId = (id: string) =>
  id.length > 18 ? id.slice(0, 11) + "…" + id.slice(-4) : id;
export const mdhm = (iso: string) =>
  `${iso.slice(5, 7)}/${iso.slice(8, 10)} ${iso.slice(11, 16)}`;
export const dayShort = (iso: string) =>
  `${+iso.slice(5, 7)}/${+iso.slice(8, 10)}`;
export function dateLabel(iso: string) {
  return `${dayShort(iso)} ${"일월화수목금토"[new Date(iso.slice(0, 10) + "T00:00:00Z").getUTCDay()]} ${iso.slice(11, 16)}`;
}
export function objectValue(
  value: JsonValue,
): { [key: string]: JsonValue } | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value
    : null;
}
export function fieldText(t: Thread): [string, string] | null {
  if (t.kind === "요청" && t.effective_deadline)
    return ["마감", dayShort(t.effective_deadline)];
  if (!t.field || t.value === null) return null;
  const v = objectValue(t.value);
  if (t.field === "deadline" && typeof t.value === "string")
    return ["마감", dayShort(t.value)];
  if (t.field === "schedule" && v && typeof v.start === "string")
    return [
      "일정",
      `${dayShort(v.start)} ${v.start.slice(11, 16)}–${typeof v.end === "string" ? v.end.slice(11, 16) : "—"}`,
    ];
  if (t.field === "assignees" && v)
    return [
      "담당자",
      [
        ...(Array.isArray(v.add) ? v.add.map((n) => "+" + String(n)) : []),
        ...(Array.isArray(v.remove)
          ? v.remove.map((n) => "−" + String(n))
          : []),
      ].join(", "),
    ];
  return [
    fieldLabel[t.field],
    typeof t.value === "string" ? t.value : JSON.stringify(t.value),
  ];
}
export function relations(rows: Thread[]) {
  const retracted = new Set(
    rows.flatMap((t) => (t.retracts ? [t.retracts] : [])),
  );
  const resolved = new Set(
    rows.flatMap((t) =>
      t.resolves && !t.retracts && !retracted.has(t.thread_id)
        ? [t.resolves]
        : [],
    ),
  );
  return {
    retracted,
    resolved,
    isOpen: (t: Thread) =>
      t.kind === "요청" &&
      !retracted.has(t.thread_id) &&
      !resolved.has(t.thread_id),
  };
}

export const incomingRefLabels = {
  resolves: "이 요청을 해결함",
  retracts: "이 Thread를 철회함",
  updates: "이 일정을 변경함",
  amends: "이 요청의 기한을 변경함",
  blocked_by: "이 요청을 기다림",
  executes: "이 Thread를 실행함",
};
