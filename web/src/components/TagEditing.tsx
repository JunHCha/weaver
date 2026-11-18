import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  editing,
  type BatchTagResult,
  type DictionaryEntry,
  type TagEdit,
  type Thread,
} from "../lib/api";
import { normalizeTag } from "../lib/tagSuggestions";
interface EditingContext {
  entries: DictionaryEntry[] | null;
  byName: Map<string, DictionaryEntry>;
  byLabel: Map<string, DictionaryEntry>;
  busy: boolean;
  result: BatchTagResult | null;
  undoTargets: string[];
  revert: () => Promise<void>;
  save: (threads: Thread[], edit: TagEdit, bulk?: boolean) => Promise<void>;
}
const Context = createContext<EditingContext | null>(null);
export const useTagEditing = () => useContext(Context);
export function TagEditingProvider({
  children,
  onSaved,
  onBusy,
}: {
  children: ReactNode;
  onSaved?: () => void;
  onBusy?: (busy: boolean) => void;
}) {
  const [entries, setEntries] = useState<DictionaryEntry[] | null>(null),
    [busy, setBusy] = useState(false);
  const [undo, setUndo] = useState<{
    ids: string[];
    until: number;
    targets: string[];
  } | null>(null);
  const [result, setResult] = useState<BatchTagResult | null>(null),
    [error, setError] = useState("");
  const lock = useRef(false),
    undoButton = useRef<HTMLButtonElement>(null);
  const byName = useMemo(
    () => new Map(entries?.map((entry) => [entry.name, entry])),
    [entries],
  );
  const byLabel = useMemo(
    () =>
      new Map(
        entries?.flatMap((entry) =>
          [entry.name, ...entry.aliases].map(
            (name) => [normalizeTag(name), entry] as const,
          ),
        ),
      ),
    [entries],
  );
  useEffect(() => {
    const controller = new AbortController();
    editing
      .dictionary(controller.signal)
      .then(setEntries)
      .catch(() => {
        if (!controller.signal.aborted)
          setError("태그 사전을 불러오지 못했습니다.");
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!undo || busy) return;
    const timer = setTimeout(
      () => setUndo(null),
      Math.max(0, undo.until - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [undo, busy]);
  useEffect(() => {
    if (
      undo &&
      ![...document.querySelectorAll("[data-id]")].some((row) =>
        undo.targets.includes(row.getAttribute("data-id") || ""),
      )
    )
      undoButton.current?.focus();
  });
  const save = useCallback(
    async (threads: Thread[], edit: TagEdit, bulk = false) => {
      if (lock.current) throw new Error("저장 중입니다.");
      lock.current = true;
      setBusy(true);
      onBusy?.(true);
      setError("");
      setResult(null);
      try {
        if (Object.keys(edit.kinds || {}).length) {
          const fresh = await editing.dictionary();
          setEntries(fresh);
          for (const name of Object.keys(edit.kinds || {})) {
            const existing = fresh.find((entry) =>
              [entry.name, ...entry.aliases].some(
                (label) => normalizeTag(label) === normalizeTag(name),
              ),
            );
            if (existing)
              throw new Error(
                `이미 등록된 항목: ${existing.name} (${existing.kind}) — 새로 불러온 사전에서 선택하세요.`,
              );
          }
        }
        let saved: BatchTagResult;
        if (bulk)
          saved = await editing.batchTags(
            threads.map((t) => t.thread_id),
            edit,
          );
        else {
          const correction = await editing.tags(threads[0].thread_id, edit);
          const changed = correction.thread_id !== threads[0].thread_id;
          saved = {
            corrections: changed ? [correction] : [],
            changed: Number(changed),
            skipped: Number(!changed),
            skipped_threads: changed
              ? []
              : [
                  {
                    thread_id: threads[0].thread_id,
                    reason: "이미 같은 태그 상태입니다.",
                  },
                ],
          };
        }
        setResult(saved);
        if (saved.corrections.length)
          setUndo({
            ids: saved.corrections.map((t) => t.thread_id),
            targets: saved.corrections.map((t) => t.correction!.target),
            until: Date.now() + 5000,
          });
        void editing
          .dictionary()
          .then(setEntries)
          .catch(() => {});
      } finally {
        lock.current = false;
        setBusy(false);
        onBusy?.(false);
        onSaved?.();
      }
    },
    [onSaved, onBusy],
  );
  const revert = async () => {
    if (!undo || lock.current) return;
    lock.current = true;
    setBusy(true);
    onBusy?.(true);
    setError("");
    const remaining = [...undo.ids];
    try {
      while (remaining.length) {
        await editing.undoTags(remaining[0]);
        remaining.shift();
      }
      setUndo(null);
      setResult(null);
    } catch (e) {
      setUndo({
        ids: remaining,
        targets: undo.targets,
        until: Date.now() + 5000,
      });
      setError(e instanceof Error ? e.message : "되돌리지 못했습니다.");
    } finally {
      lock.current = false;
      setBusy(false);
      onBusy?.(false);
      onSaved?.();
      void editing
        .dictionary()
        .then(setEntries)
        .catch(() => {});
    }
  };
  return (
    <Context.Provider
      value={{
        entries,
        byName,
        byLabel,
        busy,
        result,
        save,
        undoTargets: undo?.targets || [],
        revert,
      }}
    >
      {children}
      {undo && (
        <div className="tag-undo" role="status">
          태그 {undo.ids.length}건 변경됨{" "}
          <button
            ref={undoButton}
            className="chipbtn"
            disabled={busy}
            onClick={() => void revert()}
          >
            되돌리기
          </button>
        </div>
      )}
      {error && (
        <p className="dictionary-error tag-edit-error" role="alert">
          {error}
        </p>
      )}
    </Context.Provider>
  );
}

export function TagFeedback() {
  const result = useTagEditing()?.result;
  return (
    <>
      {result && (
        <section
          className="tag-feedback"
          aria-label="태그 편집 결과"
          role="status"
        >
          {result.changed
            ? `${result.changed}개 변경`
            : "변경된 태그가 없습니다."}{" "}
          · {result.skipped}개 건너뜀
          {result.skipped_threads.length > 0 && (
            <ul>
              {result.skipped_threads.map((row) => (
                <li key={row.thread_id}>
                  {row.thread_id}: {row.reason}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </>
  );
}
