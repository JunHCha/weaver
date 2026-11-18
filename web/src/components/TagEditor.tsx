import { createPortal } from "react-dom";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { type TagEdit, type TagKind, type Thread } from "../lib/api";
import { isHistory } from "../lib/history";
import { normalizeTag } from "../lib/tagSuggestions";
import { TagEditingProvider, TagFeedback, useTagEditing } from "./TagEditing";
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
  if (isHistory(props.thread))
    return <span>{props.thread.tags.map((tag) => `#${tag}`).join(" ")}</span>;
  return context ? (
    <Editor {...props} />
  ) : (
    <TagEditingProvider onSaved={props.onSaved}>
      <Editor {...props} />
      <TagFeedback />
    </TagEditingProvider>
  );
}
function Editor({ thread, threads, compact = false, bulk = false }: Props) {
  const {
    entries,
    byName,
    byLabel,
    busy,
    save: saveThreads,
    undoTargets,
    revert,
  } = useTagEditing()!;
  const [tags, setTags] = useState(thread.tags),
    [name, setName] = useState(""),
    [kind, setKind] = useState<TagKind | "">("");
  const [error, setError] = useState(""),
    [open, setOpen] = useState(!compact),
    [showOptions, setShowOptions] = useState(false),
    [active, setActive] = useState(-1);
  const id = useId(),
    addButton = useRef<HTMLButtonElement>(null),
    popup = useRef<HTMLDivElement>(null),
    composing = useRef(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  // Ignore snapshots arriving during a write; the next successful refresh reconciles it.
  useEffect(() => {
    if (!busy) setTags(thread.tags);
  }, [thread.tags]); // eslint-disable-line react-hooks/exhaustive-deps
  const close = () => {
    setOpen(false);
    setShowOptions(false);
    addButton.current?.focus();
  };
  useLayoutEffect(() => {
    if (!compact || !open) return;
    const place = () => {
      const anchor = addButton.current;
      if (!anchor?.isConnected) {
        setOpen(false);
        return;
      }
      const rect = anchor.getBoundingClientRect(),
        height = popup.current?.getBoundingClientRect().height || 160;
      const card = anchor.closest("article")?.getBoundingClientRect();
      const side = card && card.right + 328 < window.innerWidth;
      const left = side
        ? card.right + 8
        : Math.max(8, Math.min(rect.left, window.innerWidth - 328));
      const top = side
        ? rect.top
        : rect.top > height + 8
          ? rect.top - height - 4
          : rect.bottom + 4;
      setPosition({
        left,
        top: Math.max(8, Math.min(top, window.innerHeight - height - 8)),
      });
    };
    place();
    window.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    const observer = new ResizeObserver(place);
    if (popup.current) observer.observe(popup.current);
    const outside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !popup.current?.contains(target) &&
        !addButton.current?.contains(target)
      )
        setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => {
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", place, true);
      document.removeEventListener("pointerdown", outside);
      observer.disconnect();
    };
  }, [compact, open]);
  const normalized = normalizeTag(name),
    entry = byLabel.get(normalized);
  const options =
    entries
      ?.filter((entry) =>
        [entry.name, ...entry.aliases].some((label) =>
          normalizeTag(label).includes(normalized),
        ),
      )
      .slice(0, 12) || [];
  const save = async (edit: TagEdit, next: string[]) => {
    const previous = tags;
    setTags(next);
    setError("");
    setShowOptions(false);
    try {
      await saveThreads(threads || [thread], edit, bulk);
      setName("");
      setKind("");
      if (compact) close();
    } catch (e) {
      setTags(previous);
      setError(e instanceof Error ? e.message : "저장하지 못했습니다.");
    }
  };
  const pick = (value: string) => {
    setName(value);
    setKind("");
    setShowOptions(false);
    setActive(-1);
  };
  const form = (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (
          composing.current ||
          !normalized ||
          busy ||
          entries === null ||
          (!entry && !kind)
        )
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
      <div className="tag-combobox">
        <input
          autoFocus={compact}
          role="combobox"
          aria-label="추가할 태그"
          aria-autocomplete="list"
          aria-controls={id}
          aria-expanded={showOptions && options.length > 0}
          aria-activedescendant={
            showOptions && active >= 0 ? `${id}-${active}` : undefined
          }
          placeholder="태그 이름 또는 별칭"
          value={name}
          disabled={busy || entries === null}
          onFocus={() => setShowOptions(true)}
          onCompositionStart={() => {
            composing.current = true;
          }}
          onCompositionEnd={() => {
            composing.current = false;
          }}
          onChange={(e) => {
            setName(e.target.value);
            setKind("");
            setActive(-1);
            setShowOptions(true);
          }}
          onKeyDown={(event) => {
            if (
              event.nativeEvent.isComposing ||
              composing.current ||
              event.keyCode === 229
            )
              return;
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              setShowOptions(true);
              setActive((old) =>
                options.length
                  ? (old +
                      (event.key === "ArrowDown" ? 1 : options.length - 1) +
                      options.length) %
                    options.length
                  : -1,
              );
            }
            if (
              event.key === "Enter" &&
              showOptions &&
              active >= 0 &&
              options[active]
            ) {
              event.preventDefault();
              pick(options[active].name);
            }
            if (event.key === "Escape" && showOptions) {
              event.preventDefault();
              event.stopPropagation();
              setShowOptions(false);
            }
          }}
        />
        <ul
          id={id}
          role="listbox"
          aria-label="사전 자동완성"
          hidden={!showOptions || !options.length}
        >
          {options.map((option, i) => (
            <li
              role="option"
              id={`${id}-${i}`}
              aria-selected={active === i}
              key={option.name}
              onPointerDown={(e) => {
                e.preventDefault();
                pick(option.name);
              }}
            >
              {option.name}{" "}
              <span data-kind={option.kind}>
                {tagKinds[option.kind]}
                {option.archived ? " · 보관" : ""}
              </span>
            </li>
          ))}
        </ul>
      </div>
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
      {bulk && (
        <button
          type="button"
          className="chipbtn"
          disabled={busy || !normalized}
          onClick={() =>
            void save(
              {
                add: [],
                remove: [entry?.name || name.trim().normalize("NFC")],
              },
              tags.filter((tag) => tag !== (entry?.name || name.trim())),
            )
          }
        >
          일괄 삭제
        </button>
      )}
      {compact && (
        <button type="button" className="chipbtn" onClick={close}>
          닫기
        </button>
      )}
      {compact && error && <p role="alert">{error}</p>}
    </form>
  );
  return (
    <div
      className={"tag-editor" + (compact ? " compact" : "")}
      aria-label="태그 편집"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === "Escape" && compact) close();
        e.stopPropagation();
      }}
    >
      {!bulk && (
        <div className="editable-tags">
          {tags.map((tag) => {
            const entry = byName.get(tag);
            return (
              <span
                className={"tg" + (entry?.archived ? " archived-tag" : "")}
                data-kind={entry?.kind}
                key={tag}
              >
                #{tag}{" "}
                <span className="tag-kind-label">
                  {entry ? tagKinds[entry.kind] : "종류 미정"}
                </span>
                {entry?.archived && <span>보관</span>}
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
            );
          })}
        </div>
      )}
      {compact && (
        <button
          ref={addButton}
          type="button"
          className="chipbtn tag-plus"
          aria-label="추가할 태그 열기"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          +
        </button>
      )}
      {!bulk && undoTargets.includes(thread.thread_id) && (
        <button
          type="button"
          className="chipbtn"
          aria-label={`${thread.thread_id} 태그 되돌리기`}
          disabled={busy}
          onClick={() => void revert()}
        >
          되돌리기
        </button>
      )}
      {open &&
        (compact
          ? createPortal(
              <div
                ref={popup}
                className="tag-popup tag-editor"
                role="dialog"
                aria-label={`${thread.thread_id} 태그 추가`}
                style={{ position: "fixed", ...position }}
              >
                {form}
              </div>,
              document.body,
            )
          : form)}
      {busy && !compact && (
        <span role="status" className="mut">
          저장 중…
        </span>
      )}
      {error && (!compact || !open) && <p role="alert">{error}</p>}
    </div>
  );
}
