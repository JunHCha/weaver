import { useEffect, type Dispatch, type SetStateAction } from "react";
import type { Thread } from "../lib/api";
export function useKeyboardNavigation({
  rows,
  selected,
  focus,
  panel,
  setFocus,
  setPanel,
  setSelected,
}: {
  rows: Thread[];
  selected: string | null;
  focus: string | null;
  panel: boolean;
  setFocus: Dispatch<SetStateAction<string | null>>;
  setPanel: Dispatch<SetStateAction<boolean>>;
  setSelected: Dispatch<SetStateAction<string | null>>;
}) {
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (focus) setFocus(null);
        else if (panel) setPanel(false);
        else setSelected(null);
        return;
      }
      const target = event.target;
      if (
        event.defaultPrevented ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        (target instanceof HTMLElement &&
          ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName))
      )
        return;
      if (event.key === "j" || event.key === "k") {
        if (!rows.length) return;
        event.preventDefault();
        const old = rows.findIndex((t) => t.thread_id === selected),
          index =
            event.key === "j"
              ? Math.min(rows.length - 1, old + 1)
              : Math.max(0, old < 0 ? 0 : old - 1),
          id = rows[index].thread_id;
        setSelected(id);
        if (focus) setFocus(id);
        requestAnimationFrame(() => {
          const node = [
            ...document.querySelectorAll<HTMLElement>("[data-id]"),
          ].find((n) => n.dataset.id === id);
          node?.focus({ preventScroll: true });
          node?.scrollIntoView?.({
            block: "center",
            behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
              ? "auto"
              : "smooth",
          });
        });
      } else if (
        event.key === "Enter" &&
        selected &&
        !(target instanceof HTMLElement && target.closest("button,a,[data-id]"))
      ) {
        setPanel(true);
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [rows, selected, focus, panel, setFocus, setPanel, setSelected]);
}
