import { createPortal } from "react-dom";
import { useEffect, useId, useRef, useState } from "react";
import { type TagEdit, type TagKind, type Thread } from "../lib/api";
import { TagEditingProvider, useTagEditing } from "./TagEditing";
import { tagKinds } from "../lib/fabric";
interface Props {
  thread: Thread;
  threads?: Thread[];
  onSaved?: () => void;
  compact?: boolean;
  bulk?: boolean;
}
export function TagEditor(props: Props) {
  const context = useTagEditing();
  return context ? (
    <Editor {...props} />
  ) : (
    <TagEditingProvider onSaved={props.onSaved}>
      <Editor {...props} />
    </TagEditingProvider>
  );
}
function Editor({ thread, threads, compact = false, bulk = false }: Props) {
  const { entries, busy, save: saveThreads } = useTagEditing()!;
  const [tags, setTags] = useState(thread.tags);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<TagKind | "">("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(!compact);
  const id = useId();
  const addButton = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  useEffect(() => {
    setTags(thread.tags);
  }, [thread.tags]);
  const normalize = (value: string) =>
    value.normalize("NFC").toLocaleLowerCase().replace(/\s+/g, "");
  const normalized = normalize(name);
  const entry = entries?.find((e) =>
    [e.name, ...e.aliases].some((v) => normalize(v) === normalized),
  );
  const save = async (edit: TagEdit, next: string[]) => {
    const previous = tags;
    setTags(next);
    setError("");
    try {
      await saveThreads(threads || [thread], edit);
      setName("");
      setKind("");
      if (compact) setOpen(false);
    } catch (e) {
      setTags(previous);
      setError(e instanceof Error ? e.message : "저장하지 못했습니다.");
    }
  };

  const form = (
    <form
      className={compact ? "tag-popup tag-editor" : undefined}
      style={compact ? { position: "fixed", ...position } : undefined}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          setOpen(false);
          addButton.current?.focus();
        }
      }}
      onSubmit={(event) => {
        event.preventDefault();
        if (!normalized || busy || entries === null || (!entry && !kind))
          return;
        const canonical = entry?.name || name.trim().normalize("NFC");
        if (!bulk && tags.includes(canonical)) {
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
        autoFocus={compact}
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
        disabled={busy || !normalized || entries === null || (!entry && !kind)}
      >
        {bulk ? "일괄 추가" : "추가"}
      </button>
      {compact && (
        <button
          type="button"
          className="chipbtn"
          onClick={() => {
            setOpen(false);
            addButton.current?.focus();
          }}
        >
          닫기
        </button>
      )}
      {bulk && (
        <button
          type="button"
          className="chipbtn"
          disabled={busy || !entry}
          onClick={() =>
            entry &&
            void save(
              { add: [], remove: [entry.name] },
              tags.filter((tag) => tag !== entry.name),
            )
          }
        >
          일괄 삭제
        </button>
      )}
    </form>
  );

  return (
    <div
      className={"tag-editor" + (compact ? " compact" : "")}
      aria-label="태그 편집"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      <div className="editable-tags">
        {!bulk &&
          tags.map((tag) => (
            <span
              className="tg"
              data-kind={entries?.find((entry) => entry.name === tag)?.kind}
              key={tag}
            >
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
      {compact && (
        <button
          ref={addButton}
          className="chipbtn tag-plus"
          aria-label="추가할 태그 열기"
          aria-expanded={open}
          onClick={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            setPosition({
              left: Math.max(8, Math.min(rect.left, window.innerWidth - 310)),
              top: Math.max(
                8,
                Math.min(rect.bottom + 4, window.innerHeight - 200),
              ),
            });
            setOpen(!open);
          }}
        >
          +
        </button>
      )}
      {open && (compact ? createPortal(form, document.body) : form)}
      {busy && !compact && (
        <span role="status" className="mut">
          저장 중…
        </span>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
