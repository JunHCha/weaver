import type { TagState } from "./fabric";
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };
export type Kind =
  | "요청"
  | "결정"
  | "일정"
  | "기록"
  | "태그정정"
  | "거절"
  | "실행";
export type Stage = "spinner" | "loom";
export interface Thread {
  readonly presentation?: "fact" | "history";
  thread_id: string;
  seq: number;
  kind: Kind;
  content: string | null;
  occurred_at: string;
  recorded_at: string;
  counterpart?: string | null;
  tags: string[];
  field: "deadline" | "schedule" | "assignees" | "status" | null;
  value: JsonValue;
  source_id: string;
  actor: "human" | "ai_approved" | "system";
  direction: "받음" | "보냄" | null;
  resolves: string | null;
  retracts: string | null;
  updates: string | null;
  blocked_by: string | null;
  executes: string | null;
  correction: { target: string; add: string[]; remove: string[] } | null;
}
export interface Tag {
  archived?: boolean;
  name: string;
  kind: "project" | "area" | "person" | "organization" | "topic" | null;
}
export interface SourceView {
  id: string;
  kind: string;
  status: string;
  updated_at: string;
  stage_times: Record<string, string>;
}
export interface LogLine {
  ts: string;
  lv: string;
  lg: string;
  msg: string;
  src: string | null;
}
export type Status = Record<
  Stage,
  { last_activity: string | null; waiting_review: number }
>;
export interface FabricData {
  threads: Thread[];
  tags: Tag[];
  states: Record<string, TagState>;
  sources: SourceView[];
  status: Status;
}
async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch("/api" + path, { signal });
  if (!response.ok) throw new Error(`API ${response.status}`);
  return response.json() as Promise<T>;
}
export const api = {
  threads: (signal?: AbortSignal) =>
    get<Thread[]>("/threads?include_executions=true", signal),
  tags: (signal?: AbortSignal) => get<Tag[]>("/tags", signal),
  states: (signal?: AbortSignal) =>
    get<Record<string, TagState>>("/states", signal),
  sources: (stage: Stage, signal?: AbortSignal) =>
    get<SourceView[]>("/sources?stage=" + stage, signal),
  status: (signal?: AbortSignal) => get<Status>("/status", signal),
  logs: (stage: Stage, level: string, source: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ component: stage });
    if (level) params.set("level", level);
    if (source) params.set("source_id", source);
    return get<LogLine[]>("/logs?" + params, signal);
  },
  async fabric(signal?: AbortSignal): Promise<FabricData> {
    const [threads, tags, states, spinner, loom, status] = await Promise.all([
      api.threads(signal),
      api.tags(signal),
      api.states(signal),
      api.sources("spinner", signal),
      api.sources("loom", signal),
      api.status(signal),
    ]);
    return {
      threads,
      tags,
      states,
      status,
      sources: [
        ...new Map([...spinner, ...loom].map((s) => [s.id, s])).values(),
      ],
    };
  },
};
export type TagKind = NonNullable<Tag["kind"]>;
export interface DictionaryEntry {
  archived?: boolean;
  promoted?: boolean;
  name: string;
  kind: TagKind;
  aliases: string[];
  human_edited: boolean;
  usage_count: number;
  last_used_at?: string | null;
}
export interface BatchTagResult {
  corrections: Thread[];
  changed: number;
  skipped: number;
  skipped_threads: { thread_id: string; reason: string }[];
}
export interface TagEdit {
  add: string[];
  remove: string[];
  kinds?: Record<string, TagKind>;
}
async function write<T = void>(
  path: string,
  method: string,
  body: unknown,
): Promise<T> {
  const session = await get<{ token: string }>("/session");
  const response = await fetch("/api" + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Weaver-Token": session.token,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    throw new Error(
      response.status === 503
        ? "다른 작업이 저장 중입니다. 잠시 후 다시 시도하세요."
        : typeof detail?.detail === "string"
          ? detail.detail
          : "저장하지 못했습니다. 다시 시도하세요.",
    );
  }
  return response.json() as Promise<T>;
}
export const editing = {
  dictionary: (signal?: AbortSignal) =>
    get<DictionaryEntry[]>("/dictionary", signal),
  tags: (id: string, edit: TagEdit) =>
    write<Thread>(`/threads/${encodeURIComponent(id)}/tags`, "POST", edit),
  batchTags: (ids: string[], edit: TagEdit) =>
    write<BatchTagResult>("/threads/tags/batch", "POST", {
      thread_ids: ids,
      ...edit,
    }),
  dismissed: () => get<[string, string][]>("/dictionary/dismissed"),
  dismiss: (a: string, b: string) =>
    write("/dictionary/dismissed", "POST", { a, b }),
  undoTags: (id: string) =>
    write(`/corrections/${encodeURIComponent(id)}/undo`, "POST", {}),
  entry: (name: string, edit: { name?: string; kind?: TagKind }) =>
    write("/dictionary", "PATCH", { entry: name, ...edit }),
  alias: (name: string, alias: string, remove = false) =>
    write("/dictionary/aliases", remove ? "DELETE" : "POST", {
      entry: name,
      alias,
    }),
  merge: (name: string, target: string, kind?: TagKind) =>
    write("/dictionary/merge", "POST", {
      entry: name,
      target,
      ...(kind ? { kind } : {}),
    }),
};
