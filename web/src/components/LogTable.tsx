import { type CSSProperties } from "react";
import type { Tag, Thread } from "../lib/api";
import {
  stateSummary,
  type Fact,
  type Lane,
  type TagState,
} from "../lib/fabric";
import {
  actorLabel,
  fieldText,
  kindClass,
  kindStyle,
  mdhm,
  relations,
} from "../lib/presentation";
import {
  adjacentHistory,
  currentThread,
  isHistory,
  mentionOnly,
  valueChanges,
} from "../lib/history";
import { projectRows } from "../lib/projection";
import { Icon, References, Tags } from "./Common";
export function LogTable({
  fieldRows = [],
  selectedTags = [],
  rows,
  threads,
  tags,
  lanes,
  groups,
  group,
  states,
  selected,
  onPick,
}: {
  fieldRows?: Thread[];
  selectedTags?: string[];
  rows: Thread[];
  threads: Thread[];
  tags: Tag[];
  lanes: Lane[];
  groups: (t: Fact) => string[];
  group: string;
  states: Record<string, TagState>;
  selected: string | null;
  onPick: (id: string) => void;
}) {
  const state = relations(threads),
    grouped = group !== "none";
  return (
    <div className="scroller" id="scr">
      <div
        className="ltable"
        id="lt"
        style={{ "--gw": "36px" } as CSSProperties}
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
          const section = adjacentHistory(
            rows.filter((t) => groups(t).includes(lane.key)),
          );
          const changes = valueChanges(
            fieldRows.filter((t) => groups(t).includes(lane.key)),
            threads,
            tags,
          );
          const header = grouped || changes.length > 0 ? 34 : 0;
          const heights = section.map((t) =>
            isHistory(t) || state.retracted.has(t.thread_id) ? 28 : 34,
          );
          const centers = heights.map(
            (height, i) =>
              header +
              heights.slice(0, i).reduce((sum, h) => sum + h, 0) +
              height / 2,
          );
          const summary =
            states[lane.key] ||
            projectRows(
              threads.filter((t) => groups(t).includes(lane.key)),
              threads,
            );
          return (
            <section className="lsec" key={lane.key}>
              {(grouped || changes.length > 0) && (
                <div className="lsech">
                  <b>{lane.key || "전체"}</b>
                  <span className="mut">{section.length}</span>
                  {!!changes.length && (
                    <details className="value-history">
                      <summary>변경 {changes.length}</summary>
                      <ul>
                        {changes.map(({ thread, label }) => (
                          <li key={thread.thread_id}>
                            <time>{mdhm(thread.recorded_at)}</time>{" "}
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
                    </details>
                  )}
                  <span className="sums">
                    {(grouped ? stateSummary(summary) : []).map(([k, v]) => (
                      <span className="sm" key={k}>
                        <b>{k}</b> {v}
                      </span>
                    ))}
                  </span>
                </div>
              )}
              {section.map((t) => {
                const field = fieldText(currentThread(t, threads, tags));
                return (
                  <div
                    key={t.thread_id}
                    className={[
                      "lrow",
                      isHistory(t) || state.retracted.has(t.thread_id)
                        ? "history-item"
                        : "",
                      mentionOnly(t, selectedTags, tags) ? "mention-only" : "",
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
                    <div />
                    <div className="mut">{mdhm(t.occurred_at)}</div>
                    <div>
                      <span className="kw">{t.kind}</span>
                    </div>
                    <div className="lc" title={t.content || ""}>
                      {t.content || <span className="mut">내용 없음</span>}
                      {mentionOnly(t, selectedTags, tags) && (
                        <span className="mention-mark"> 언급</span>
                      )}
                      {isHistory(t) && <span className="mut"> 이력</span>}
                      {t.kind === "요청" && state.resolved.has(t.thread_id) && (
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
                          <Icon name={t.direction === "받음" ? "in" : "out"} />
                          {t.direction}
                        </span>
                      )}
                    </div>
                    <div className="mut">
                      <Tags thread={t} tags={tags} />
                    </div>
                    <div>{actorLabel[t.actor]}</div>
                    <div className="mut">
                      <References
                        thread={t}
                        threads={threads}
                        numbers={false}
                      />
                    </div>
                    <div className="r mut">{t.seq}</div>
                  </div>
                );
              })}
              <svg
                className="lsvg"
                width={36}
                height={header + heights.reduce((sum, h) => sum + h, 0)}
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
                      kindClass[t.kind] === "hol"
                        ? "var(--bg)"
                        : `var(--c-${kindClass[t.kind]})`
                    }
                    stroke="var(--bg)"
                    strokeWidth="1.5"
                  />
                ))}
              </svg>
            </section>
          );
        })}
      </div>
    </div>
  );
}
