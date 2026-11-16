import { useEffect, useId, useState } from "react";
import {
  editing,
  type DictionaryEntry,
  type TagEdit,
  type TagKind,
  type Thread,
} from "../lib/api";
import { tagKinds } from "../lib/fabric";
export function TagEditor({
  thread,
  onSaved,
}: {
  thread: Thread;
  onSaved?: () => void;
}) {
  const [entries, setEntries] = useState<DictionaryEntry[] | null>(null);
  const [tags, setTags] = useState(thread.tags);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<TagKind | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const id = useId();
  useEffect(() => {
    setTags(thread.tags);
  }, [thread.tags]);
  useEffect(() => {
    const controller = new AbortController();
    editing
      .dictionary(controller.signal)
      .then(setEntries)
      .catch(() => {
        if (!controller.signal.aborted)
          setError(
            "태그 사전을 불러오지 못했습니다. 상세 패널을 다시 열어 주세요.",
          );
      });
    return () => controller.abort();
  }, []);
  const normalized = name.trim().normalize("NFC").toLocaleLowerCase();
  const entry = entries?.find((e) =>
    [e.name, ...e.aliases].some(
      (v) => v.normalize("NFC").toLocaleLowerCase() === normalized,
    ),
  );
  const save = async (edit: TagEdit, next: string[]) => {
    const previous = tags;
    setTags(next);
    setBusy(true);
    setError("");
    try {
      await editing.tags(thread.thread_id, edit);
      setName("");
      setKind("");
      onSaved?.();
      void editing
        .dictionary()
        .then(setEntries)
        .catch(() => {
          /* The saved tags remain valid if the dictionary refresh fails. */
        });
    } catch (e) {
      setTags(previous);
      setError(e instanceof Error ? e.message : "저장하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="tag-editor" aria-label="태그 편집">
      <div className="editable-tags">
        {tags.map((tag) => (
          <span className="tg" key={tag}>
            #{tag}
            <button
              type="button"
              aria-label={`${tag} 태그 삭제`}
              disabled={busy}
              onClick={() =>
                void save(
                  { add: [], remove: [tag] },
                  tags.filter((t) => t !== tag),
                )
              }
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!normalized || busy || entries === null || (!entry && !kind))
            return;
          const canonical = entry?.name || name.trim().normalize("NFC");
          if (tags.includes(canonical)) {
            setError("이미 추가한 태그입니다.");
            return;
          }
          void save(
            {
              add: [canonical],
              remove: [],
              kinds: kind ? { [canonical]: kind } : {},
            },
            [...tags, canonical],
          );
        }}
      >
        <input
          aria-label="추가할 태그"
          placeholder="태그 이름 또는 별칭"
          list={id}
          value={name}
          disabled={busy || entries === null}
          onChange={(e) => {
            setName(e.target.value);
            setKind("");
          }}
        />
        <datalist id={id}>
          {entries?.flatMap((e) => [
            <option key={e.name} value={e.name} />,
            ...e.aliases.map((alias) => (
              <option key={e.name + alias} value={alias}>
                {e.name}
              </option>
            )),
          ])}
        </datalist>
        {!!normalized && entries !== null && !entry && (
          <label className="new-tag-kind">
            새 태그 종류{" "}
            <select
              aria-label="새 태그 종류"
              value={kind}
              onChange={(e) => setKind(e.target.value as TagKind)}
            >
              <option value="">종류 선택</option>
              {Object.entries(tagKinds).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          type="submit"
          className="chipbtn"
          disabled={
            busy || !normalized || entries === null || (!entry && !kind)
          }
        >
          추가
        </button>
      </form>
      {busy && (
        <span role="status" className="mut">
          저장 중…
        </span>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
