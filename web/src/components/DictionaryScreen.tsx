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
            최근 {entry.last_used_at ? mdhm(entry.last_used_at) : "사용 없음"}
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
            {confirmMerge && (
              <span role="group" aria-label="합치기 확인">
                {entry.name}을(를) {target}(으)로 합칩니다. 대상 종류와 두
                항목의 별칭을 유지합니다.{" "}
                <button
                  className="chipbtn"
                  disabled={busy}
                  onClick={() =>
                    void onEdit(() => editing.merge(entry.name, target))
                  }
                >
                  합치기 확인
                </button>
                <button
                  className="chipbtn"
                  onClick={() => setConfirmMerge(false)}
                >
                  취소
                </button>
              </span>
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
  const [expanded, setExpanded] = useState<string | null>(null);
  const load = useCallback(async () => {
    setEntries(await editing.dictionary());
  }, []);
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
  const suggestions = useMemo(() => tagSuggestions(entries || []), [entries]);
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
        {entries &&
          suggestions
            .filter(({ source, target }) =>
              [
                source.name,
                target.name,
                ...source.aliases,
                ...target.aliases,
              ].some((name) =>
                name.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
              ),
            )
            .map(({ source, target, reason }) => (
              <section
                className="merge-suggestion"
                aria-label={`${source.name} ${target.name} 합치기 제안`}
                key={source.name + "|" + target.name}
              >
                <b>합칠까요?</b> {source.name} → {target.name}{" "}
                <span className="mut">{reason}</span>
                <button
                  className="chipbtn"
                  disabled={busy}
                  onClick={() =>
                    void edit(() => editing.merge(source.name, target.name))
                  }
                >
                  합치기
                </button>
                <button
                  className="chipbtn"
                  disabled={busy}
                  onClick={() =>
                    void edit(() => editing.merge(source.name, target.name))
                  }
                >
                  별칭으로 넣기
                </button>
                <small className="mut">
                  {target.name}의 종류를 유지하고 {source.name}을 별칭으로
                  남깁니다.
                </small>
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
