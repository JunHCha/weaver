import { TagEditor } from "./TagEditor";
import { useTagEditing } from "./TagEditing";
import { useMemo, useState, type CSSProperties } from "react";
import type { Thread } from "../lib/api";
import {
  stateSummary,
  type Fact,
  type Lane,
  type TagState,
} from "../lib/fabric";
import { actorLabel, kindClass, kindStyle, mdhm } from "../lib/presentation";
import {
  adjacentHistory,
  distribute,
  isHistory,
  mentionMatch,
  type ThreadView,
  type ValueChange,
} from "../lib/history";
import { Icon, References, Tags } from "./Common";
import { HistoryChanges } from "./HistoryChanges";
export function LogTable({
  changes,
  view,
  selectedTags = [],
  rows,
  lanes,
  groups,
  group,
  states,
  selected,
  onPick,
}: {
  changes: ValueChange[];
  view: ThreadView;
  selectedTags?: string[];
  rows: Thread[];
  lanes: Lane[];
  groups: (t: Fact) => string[];
  group: string;
  states: Record<string, TagState>;
  selected: string | null;
  onPick: (id: string) => void;
}) {
  const editing = useTagEditing();
  const [checked, setChecked] = useState<string[]>([]);
  const checkedIds = new Set(checked);
  const selectedRows = rows.filter(
    (row) => checkedIds.has(row.thread_id) && !isHistory(row),
  );
  const grouped = group !== "none",
    targeted = group === "project" || group === "area";
  const sections = useMemo(() => distribute(rows, groups), [rows, groups]);
  const changesByLane = useMemo(
    () =>
      targeted
        ? distribute(changes, ({ thread }) => groups(thread))
        : new Map<string, ValueChange[]>(),
    [changes, groups, targeted],
  );
  const state = view.relation;
  return (
    <div className="scroller" id="scr">
      {editing && (
        <section className="bulk-tags" aria-label="태그 일괄 편집">
          <label>
            <input
              type="checkbox"
              aria-label="모든 Thread 선택"
              checked={
                rows.some((t) => !isHistory(t)) &&
                rows
                  .filter((t) => !isHistory(t))
                  .every((t) => checkedIds.has(t.thread_id))
              }
              onChange={(e) =>
                setChecked(
                  e.target.checked
                    ? rows.filter((t) => !isHistory(t)).map((t) => t.thread_id)
                    : [],
                )
              }
            />{" "}
            전체 선택
          </label>
          <span>선택한 {selectedRows.length}개에</span>
          {selectedRows.length > 500 && (
            <span role="alert">
              한 번에 최대 500개 Thread를 편집할 수 있습니다. 선택을 줄여
              주세요.
            </span>
          )}
          {selectedRows.length > 0 && selectedRows.length <= 500 && (
            <TagEditor
              key={selectedRows.map((t) => t.thread_id).join("|")}
              thread={selectedRows[0]}
              threads={selectedRows}
              bulk
            />
          )}
        </section>
      )}

      {!targeted && changes.length > 0 && (
        <section className="unassigned-history" aria-label="값 변경 이력">
          <b>값 변경 이력</b>
          <HistoryChanges changes={changes} onPick={onPick} showTarget />
        </section>
      )}
      <div
        className="ltable"
        id="lt"
        style={{ "--gw": editing ? "58px" : "36px" } as CSSProperties}
      >
        <div className="lrow lhead">
          {[
            "",
            "시각",
            "유형",
            "내용",
            "필드/값",
            "방향",
            "태그",
            "행위자",
            "참조",
            "seq",
          ].map((v, i) => (
            <div key={i} className={i === 9 ? "r" : ""}>
              {v}
            </div>
          ))}
        </div>
        {lanes.map((lane) => {
          const section = adjacentHistory(sections.get(lane.key) || []);
          const laneChanges =
            targeted && !lane.none ? changesByLane.get(lane.key) || [] : [];
          const summary = targeted && !lane.none ? states[lane.key] : undefined;
          let height = 0;
          const centers = section.map((t) => {
            const h =
              isHistory(t) || state.retracted.has(t.thread_id) ? 30 : 34;
            const center = height + h / 2;
            height += h;
            return center;
          });
          return (
            <section className="lsec" key={lane.key}>
              {grouped && (
                <div className="lsech">
                  <b>{lane.key}</b>
                  <span className="mut">{section.length}</span>
                  {summary && (
                    <span className="sums">
                      {stateSummary(summary).map(([k, v]) => (
                        <span className="sm" key={k}>
                          <b>{k}</b> {v}
                        </span>
                      ))}
                    </span>
                  )}
                  <HistoryChanges changes={laneChanges} onPick={onPick} />
                </div>
              )}
              <div className="lsection-rows">
                {section.map((t) => {
                  const field = view.fields.get(t.thread_id),
                    history = isHistory(t) || state.retracted.has(t.thread_id),
                    mention = mentionMatch(t, selectedTags, view.peopleTags);
                  return (
                    <div
                      key={t.thread_id}
                      className={[
                        "lrow",
                        history ? "history-item" : "",
                        mention ? "mention-only" : "",
                        "k-" + kindClass[t.kind],
                        kindClass[t.kind] === "hol" ? "hol" : "",
                        selected === t.thread_id ? "sel" : "",
                        state.retracted.has(t.thread_id) ? "retracted" : "",
                      ].join(" ")}
                      data-id={t.thread_id}
                      tabIndex={0}
                      style={kindStyle(t.kind)}
                      onClick={() => onPick(t.thread_id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.stopPropagation();
                          onPick(t.thread_id);
                        }
                      }}
                    >
                      <div
                        onClick={(e) => e.stopPropagation()}
                        onKeyDown={(e) => e.stopPropagation()}
                      >
                        {editing && !isHistory(t) && (
                          <input
                            type="checkbox"
                            className="thread-check"
                            aria-label={`${t.thread_id} Thread 선택`}
                            checked={checkedIds.has(t.thread_id)}
                            onChange={(e) =>
                              setChecked((old) =>
                                e.target.checked
                                  ? [...old, t.thread_id]
                                  : old.filter((id) => id !== t.thread_id),
                              )
                            }
                          />
                        )}
                      </div>
                      <div className="mut">{mdhm(t.occurred_at)}</div>
                      <div>
                        {history && <span className="history-type">이력</span>}{" "}
                        <span className="kw">{t.kind}</span>
                      </div>
                      <div className="lc" title={t.content || ""}>
                        {t.content || <span className="mut">내용 없음</span>}
                        {mention && <span className="mention-mark">언급</span>}
                        {t.kind === "요청" &&
                          state.resolved.has(t.thread_id) && (
                            <span className="mark"> 해결됨</span>
                          )}
                      </div>
                      <div>
                        {field ? (
                          <>
                            <span className="mut">{field[0]}</span> {field[1]}
                          </>
                        ) : (
                          <span className="mut">—</span>
                        )}
                      </div>
                      <div className="mut">
                        {t.direction && (
                          <span className="dir">
                            <Icon
                              name={t.direction === "받음" ? "in" : "out"}
                            />
                            {t.direction}
                          </span>
                        )}
                      </div>
                      <div className="mut">
                        {editing && !isHistory(t) ? (
                          <TagEditor thread={t} compact />
                        ) : (
                          <Tags thread={t} knownTags={view.knownTags} />
                        )}
                      </div>
                      <div>{actorLabel[t.actor]}</div>
                      <div className="mut">
                        <References
                          thread={t}
                          byId={view.byId}
                          numbers={false}
                        />
                      </div>
                      <div className="r mut">{t.seq}</div>
                    </div>
                  );
                })}
                <svg
                  className="lsvg"
                  width={editing ? 58 : 36}
                  height={height}
                  aria-hidden="true"
                >
                  {section.length > 1 && (
                    <line
                      x1={14}
                      x2={14}
                      y1={centers[0]}
                      y2={centers.at(-1)}
                      stroke="var(--thread)"
                      strokeWidth="1.25"
                      opacity=".55"
                    />
                  )}
                  {section.map((t, i) => (
                    <circle
                      key={t.thread_id}
                      cx={14}
                      cy={centers[i]}
                      r="3.6"
                      fill={
                        isHistory(t) || kindClass[t.kind] === "hol"
                          ? "var(--bg)"
                          : `var(--c-${kindClass[t.kind]})`
                      }
                      stroke="var(--muted)"
                      strokeWidth="1"
                    />
                  ))}
                </svg>
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
