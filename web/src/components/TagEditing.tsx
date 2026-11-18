import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  editing,
  type DictionaryEntry,
  type TagEdit,
  type Thread,
} from "../lib/api";

interface EditingContext {
  entries: DictionaryEntry[] | null;
  busy: boolean;
  save: (threads: Thread[], edit: TagEdit) => Promise<void>;
}
const Context = createContext<EditingContext | null>(null);
export const useTagEditing = () => useContext(Context);

export function TagEditingProvider({
  children,
  onSaved,
}: {
  children: ReactNode;
  onSaved?: () => void;
}) {
  const [entries, setEntries] = useState<DictionaryEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [undo, setUndo] = useState<{ ids: string[]; until: number } | null>(
    null,
  );
  const [error, setError] = useState("");
  const lock = useRef(false);
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
  const save = useCallback(
    async (threads: Thread[], edit: TagEdit) => {
      if (lock.current) throw new Error("저장 중입니다.");
      lock.current = true;
      setBusy(true);
      setError("");
      const ids: string[] = [];
      try {
        for (const thread of threads) {
          const result = await editing.tags(thread.thread_id, edit);
          if (result?.correction && result.thread_id !== thread.thread_id)
            ids.push(result.thread_id);
        }
      } finally {
        // Keep successful corrections undoable even when a later row failed.
        if (ids.length) setUndo({ ids, until: Date.now() + 5000 });
        onSaved?.();
        void editing
          .dictionary()
          .then(setEntries)
          .catch(() => {});
        lock.current = false;
        setBusy(false);
      }
    },
    [onSaved],
  );
  const revert = async () => {
    if (!undo || lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    const remaining = [...undo.ids];
    try {
      while (remaining.length) {
        await editing.undoTags(remaining[0]);
        remaining.shift();
      }
      setUndo(null);
    } catch (e) {
      setUndo({ ids: remaining, until: Date.now() + 5000 });
      setError(e instanceof Error ? e.message : "되돌리지 못했습니다.");
    } finally {
      onSaved?.();
      void editing
        .dictionary()
        .then(setEntries)
        .catch(() => {});
      lock.current = false;
      setBusy(false);
    }
  };
  return (
    <Context.Provider value={{ entries, busy, save }}>
      {children}
      {undo && (
        <div className="tag-undo" role="status">
          태그 {undo.ids.length}건 변경됨{" "}
          <button
            className="chipbtn"
            disabled={busy}
            onClick={() => void revert()}
          >
            되돌리기
          </button>
        </div>
      )}
      {error && (
        <p className="dictionary-error" role="alert">
          {error}
        </p>
      )}
    </Context.Provider>
  );
}
