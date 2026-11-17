import type { Thread } from "../lib/api";
import { kindClass, kindStyle, refs, refLabels } from "../lib/presentation";
export function Icon({
  name,
  size = 12,
}: {
  name: "copy" | "x" | "chev" | "search" | "in" | "out" | (typeof refs)[number];
  size?: number;
}) {
  const paths = {
    copy: "M4 4h6v6H4zM8 2H2v6",
    x: "M3 3l6 6m0-6L3 9",
    chev: "M3 4.5L6 7.5L9 4.5",
    search: "M7.6 7.6l3 3M8.4 5.2a3.2 3.2 0 1 1-6.4 0 3.2 3.2 0 0 1 6.4 0",
    in: "M9 3L3 9M3 4.5V9H7.5",
    out: "M3 9L9 3M4.5 3H9V7.5",
    resolves: "M2 6.5L5 9.5L10 3",
    retracts: "M4 3L2 5L4 7M2 5H7A2.6 2.6 0 0 1 7 10.2H5",
    updates: "M10 6A4 4 0 1 1 8.6 2.9M10 2V4.6H7.4",
    blocked_by: "M3 5.5h6V10H3zM4.5 5.5V4a1.5 1.5 0 0 1 3 0v1.5",
    executes: "M6.6 1.5L3 6.6H6L5.4 10.5L9 5.4H6z",
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
export function Tags({
  thread,
  knownTags,
}: {
  thread: Thread;
  knownTags: ReadonlySet<string>;
}) {
  return (
    <>
      {thread.tags.map((name, i) => (
        <span key={name}>
          {i > 0 ? " " : ""}
          <span
            className={"tg" + (knownTags.has(name) ? "" : " unk")}
            title={
              knownTags.has(name)
                ? undefined
                : "종류가 정해지지 않은 태그 — wiki에서 종류를 정하세요"
            }
          >
            #{name}
          </span>
        </span>
      ))}
    </>
  );
}
export function KindWord({ thread }: { thread: Thread }) {
  return (
    <span
      className={"kind " + (kindClass[thread.kind] === "hol" ? "hollow" : "")}
      style={kindStyle(thread.kind)}
    >
      <i className="kdot" />
      {thread.kind}
    </span>
  );
}
export function References({
  thread,
  byId,
  numbers = true,
}: {
  thread: Thread;
  byId: ReadonlyMap<string, Thread>;
  numbers?: boolean;
}) {
  return (
    <span className="rels">
      {refs
        .filter((key) => thread[key])
        .map((key) => (
          <span
            key={key}
            className="rel"
            title={`${refLabels[key]} seq ${byId.get(thread[key]!)?.seq ?? "?"}`}
          >
            <Icon name={key} />
            {numbers ? byId.get(thread[key]!)?.seq : null}
          </span>
        ))}
    </span>
  );
}
export function Toggle({
  label,
  on,
  onChange,
  id,
}: {
  label: string;
  on: boolean;
  onChange: () => void;
  id?: string;
}) {
  return (
    <button
      id={id}
      className="toggle"
      type="button"
      aria-pressed={on}
      onClick={onChange}
    >
      <i />
      {label}
    </button>
  );
}
