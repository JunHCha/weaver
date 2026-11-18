import { tagSuggestions } from "../lib/tagSuggestions";
import { mdhm } from "../lib/presentation";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { editing, type DictionaryEntry, type TagKind } from "../lib/api";
import { tagKinds } from "../lib/fabric";
function MergeDialog({
  source,
  target,
  busy,
  onClose,
  onConfirm,
}: {
  source: DictionaryEntry;
  target: DictionaryEntry;
  busy: boolean;
  onClose: () => void;
  onConfirm: (kind?: TagKind) => void;
}) {
  const [kind, setKind] = useState<TagKind | "">("");
  const dialog = useRef<HTMLDialogElement>(null);
  const different = source.kind !== target.kind;
  useEffect(() => {
    const node = dialog.current;
    if (node?.showModal) node.showModal();
    else node?.setAttribute("open", "");
    return () => {
      if (node?.open && node.close) node.close();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      role="dialog"
      aria-modal="true"
      aria-label="합치기 확인"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onClose();
        }
        if (e.key === "Tab") {
          const controls = [
            ...e.currentTarget.querySelectorAll<HTMLElement>(
              "button:not(:disabled),select",
            ),
          ];
          const first = controls[0],
            last = controls.at(-1);
          if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last?.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first?.focus();
          }
        }
      }}
    >
      <h2>합치기 확인</h2>
      <p>
        <b>{target.name}</b> 이름이 남고 <b>{source.name}</b>은 별칭이 됩니다.
      </p>
      {different ? (
        <label>
          합친 뒤 종류를 직접 선택하세요{" "}
          <select
            autoFocus
            aria-label="합친 뒤 종류"
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
      ) : (
        <p>합친 뒤 종류: {tagKinds[target.kind]}</p>
      )}
      <p>두 항목의 별칭과 기존 참조를 함께 유지합니다.</p>
      <button
        className="chipbtn"
        autoFocus={!different}
        disabled={busy || (different && !kind)}
        onClick={() => onConfirm(kind || undefined)}
      >
        합치기 확인
      </button>
      <button className="chipbtn" disabled={busy} onClick={onClose}>
        취소
      </button>
    </dialog>
  );
}
function EntryRow({
  entry,
  entries,
  onEdit,
  busy,
  expanded,
  onToggle,
}: {
  entry: DictionaryEntry;
  entries: DictionaryEntry[];
  onEdit: (action: () => Promise<void>) => Promise<void>;
  busy: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  const editorId = useId();
  const summary = useRef<HTMLButtonElement>(null);
  const [name, setName] = useState(entry.name);
  const [alias, setAlias] = useState("");
  const [target, setTarget] = useState("");
  const [confirmMerge, setConfirmMerge] = useState(false);
  return (
    <article
      className="dictionary-entry"
      aria-label={entry.name}
      onKeyDown={(event) => {
        if (event.key === "Escape" && expanded) {
          event.stopPropagation();
          onToggle();
          summary.current?.focus();
        }
      }}
    >
      <button
        type="button"
        ref={summary}
        className="dictionary-row"
        aria-label={`${entry.name} 편집`}
        aria-expanded={expanded}
        aria-controls={editorId}
        onClick={onToggle}
      >
        <span className="dictionary-name">{entry.name}</span>
        <span className="dictionary-kind" data-kind={entry.kind}>
          {tagKinds[entry.kind]}
          {entry.archived ? " · 보관" : ""}
        </span>
        <span className="dictionary-alias-summary mut">
          {entry.aliases.join(" · ") || "별칭 없음"}
        </span>
        <span className="dictionary-usage mut">
          <span>Thread {entry.usage_count}개</span>
          <br />
          <span>
            최근{" "}
            {entry.last_used_at
              ? entry.last_used_at.slice(0, 4) + "/" + mdhm(entry.last_used_at)
              : "사용 없음"}
          </span>
        </span>
      </button>
      {expanded && (
        <div className="dictionary-edit" id={editorId}>
          <div className="dictionary-main">
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void onEdit(() =>
                  editing.entry(entry.name, { name: name.trim() }),
                );
              }}
            >
              <input
                aria-label={`${entry.name} 이름`}
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={busy}
              />
              <button
                className="chipbtn"
                disabled={busy || !name.trim() || name === entry.name}
              >
                이름 저장
              </button>
            </form>
            <select
              aria-label={`${entry.name} 종류`}
              value={entry.kind}
              disabled={busy}
              onChange={(e) =>
                void onEdit(() =>
                  editing.entry(entry.name, {
                    kind: e.target.value as TagKind,
                  }),
                )
              }
            >
              {Object.entries(tagKinds).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <span className="mut">Thread {entry.usage_count}개</span>
            {entry.human_edited && <span className="mut">직접 편집</span>}
          </div>
          <div className="dictionary-aliases">
            <span className="mut">별칭</span>
            {entry.aliases.length === 0 && (
              <span className="mut">등록된 별칭 없음</span>
            )}
            {entry.aliases.map((a) => (
              <span className="tg" key={a}>
                {a}
                <button
                  type="button"
                  aria-label={`${a} 별칭 삭제`}
                  disabled={busy}
                  onClick={() =>
                    void onEdit(() => editing.alias(entry.name, a, true))
                  }
                >
                  ×
                </button>
              </span>
            ))}
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void onEdit(async () => {
                  await editing.alias(entry.name, alias.trim());
                  setAlias("");
                });
              }}
            >
              <input
                aria-label={`${entry.name} 새 별칭`}
                placeholder="별칭 추가"
                value={alias}
                disabled={busy}
                onChange={(e) => setAlias(e.target.value)}
              />
              <button className="chipbtn" disabled={busy || !alias.trim()}>
                별칭 추가
              </button>
            </form>
          </div>
          <div className="dictionary-merge">
            <select
              aria-label={`${entry.name} 합칠 대상`}
              value={target}
              disabled={busy}
              onChange={(e) => {
                setTarget(e.target.value);
                setConfirmMerge(false);
              }}
            >
              <option value="">합칠 대상 선택</option>
              {entries
                .filter((e) => e.name !== entry.name)
                .map((e) => (
                  <option key={e.name} value={e.name}>
                    {e.name}
                  </option>
                ))}
            </select>
            <button
              type="button"
              className="chipbtn"
              disabled={busy || !target}
              onClick={() => setConfirmMerge(true)}
            >
              합치기
            </button>
            {confirmMerge && entries.find((e) => e.name === target) && (
              <MergeDialog
                source={entry}
                target={entries.find((e) => e.name === target)!}
                busy={busy}
                onClose={() => setConfirmMerge(false)}
                onConfirm={(kind) =>
                  void onEdit(async () => {
                    await editing.merge(entry.name, target, kind);
                    setConfirmMerge(false);
                  })
                }
              />
            )}
          </div>
        </div>
      )}
    </article>
  );
}
export function DictionaryScreen({ onSaved }: { onSaved?: () => void }) {
  const [entries, setEntries] = useState<DictionaryEntry[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState<[string, string][] | null>(null);
  const [pending, setPending] = useState<{
    source: DictionaryEntry;
    target: DictionaryEntry;
  } | null>(null);
  const mergeTrigger = useRef<HTMLButtonElement | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const load = useCallback(async () => {
    const [entries, ignored] = await Promise.all([
      editing.dictionary(),
      editing.dismissed(),
    ]);
    setEntries(entries);
    setDismissed(ignored);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void editing
      .dismissed()
      .then(setDismissed)
      .catch(() => setError("무시한 제안을 불러오지 못했습니다."));
    editing
      .dictionary(controller.signal)
      .then(setEntries)
      .catch(() => {
        if (!controller.signal.aborted)
          setError("태그 사전을 불러오지 못했습니다.");
      });
    return () => controller.abort();
  }, []);
  const edit = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await load();
      onSaved?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "저장하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };
  const suggestions = useMemo(
    () => (dismissed ? tagSuggestions(entries || [], dismissed) : []),
    [entries, dismissed],
  );
  const filtered = entries?.filter((e) =>
    [e.name, ...e.aliases].some((name) =>
      name.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
    ),
  );
  return (
    <section className="screen dictionary-screen">
      <div className="pagehead">
        <h1>태그 사전</h1>
        <span className="count">{entries?.length ?? 0}개</span>
      </div>
      <div className="toolbar">
        <label className="search">
          <input
            type="search"
            aria-label="사전 검색"
            placeholder="이름 또는 별칭 검색"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <span className="mut">
          이름을 바꾸거나 합치면 이전 태그도 같은 이름으로 표시됩니다.
        </span>
      </div>
      {error && (
        <div className="dictionary-error" role="alert">
          {error}{" "}
          <button
            className="chipbtn"
            onClick={() => {
              setError("");
              void load().catch(() =>
                setError("태그 사전을 불러오지 못했습니다."),
              );
            }}
          >
            다시 불러오기
          </button>
        </div>
      )}
      {busy && <span role="status">저장 중…</span>}
      <div className="scroller dictionary-list">
        {pending && (
          <MergeDialog
            source={pending.source}
            target={pending.target}
            busy={busy}
            onClose={() => {
              setPending(null);
              mergeTrigger.current?.focus();
            }}
            onConfirm={(kind) =>
              void edit(async () => {
                await editing.merge(
                  pending.source.name,
                  pending.target.name,
                  kind,
                );
                setPending(null);
                mergeTrigger.current?.focus();
              })
            }
          />
        )}
        {suggestions
          .filter((group) =>
            group.entries.some((entry) =>
              [entry.name, ...entry.aliases].some((name) =>
                name.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
              ),
            ),
          )
          .map((group) => (
            <section
              className="suggestion-group"
              aria-label="비슷한 태그 묶음"
              key={group.entries
                .map((e) => e.name)
                .sort()
                .join("|")}
            >
              <h2>합칠까요? {group.entries.map((e) => e.name).join(" · ")}</h2>
              {group.pairs.map(({ source, target, reason }) => (
                <div
                  className="merge-suggestion"
                  role="region"
                  aria-label={`${source.name} ${target.name} 합치기 제안`}
                  key={source.name + "|" + target.name}
                >
                  {source.name} → {target.name}{" "}
                  <span className="mut">
                    {reason} · {tagKinds[target.kind]}
                  </span>
                  <button
                    className="chipbtn"
                    disabled={busy}
                    onClick={(e) => {
                      mergeTrigger.current = e.currentTarget;
                      setPending({ source, target });
                    }}
                  >
                    합치기
                  </button>
                  <button
                    className="chipbtn"
                    disabled={busy}
                    onClick={() =>
                      void edit(() => editing.dismiss(source.name, target.name))
                    }
                  >
                    무시
                  </button>
                </div>
              ))}
            </section>
          ))}

        {entries === null ? (
          <p role="status">불러오는 중…</p>
        ) : !entries.length ? (
          <div className="empty">
            아직 등록된 태그가 없습니다. Thread 상세에서 태그를 추가하면 사전에
            등록됩니다.
          </div>
        ) : !filtered?.length ? (
          <div className="empty">
            검색 결과가 없습니다. 다른 이름이나 별칭으로 검색해 보세요.
          </div>
        ) : (
          Object.entries(tagKinds).map(([kind, label]) => {
            const rows = filtered.filter((e) => e.kind === kind);
            return rows.length ? (
              <section className="dictionary-group" key={kind}>
                <h2>
                  {label} <span className="mut">{rows.length}</span>
                </h2>
                {rows.map((entry) => (
                  <EntryRow
                    key={entry.name}
                    entry={entry}
                    entries={entries}
                    onEdit={edit}
                    busy={busy}
                    expanded={expanded === entry.name}
                    onToggle={() =>
                      setExpanded((old) =>
                        old === entry.name ? null : entry.name,
                      )
                    }
                  />
                ))}
              </section>
            ) : null;
          })
        )}
      </div>
    </section>
  );
}
