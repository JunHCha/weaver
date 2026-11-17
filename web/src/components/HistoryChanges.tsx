import { useEffect, useRef, useState } from "react";
import type { ValueChange } from "../lib/history";
import { mdhm } from "../lib/presentation";

export function HistoryChanges({
  changes,
  onPick,
  showTarget = false,
}: {
  changes: ValueChange[];
  onPick: (id: string) => void;
  showTarget?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => {
      if (e.target instanceof Node && !ref.current?.contains(e.target))
        setOpen(false);
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        ref.current?.querySelector("summary")?.focus();
        e.stopPropagation();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open]);
  if (!changes.length) return null;
  return (
    <details
      className="value-history"
      ref={ref}
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary
        onClick={(e) => {
          e.preventDefault();
          setOpen((old) => !old);
        }}
      >
        변경 {changes.length}
      </summary>
      {open && (
        <ul>
          {changes.map(({ thread, label, target }) => (
            <li key={thread.thread_id}>
              <time dateTime={thread.recorded_at}>
                {mdhm(thread.recorded_at)}
              </time>{" "}
              {showTarget && <span className="change-target">{target} · </span>}
              <button
                type="button"
                className="linkbtn"
                onClick={() => onPick(thread.thread_id)}
              >
                {label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
