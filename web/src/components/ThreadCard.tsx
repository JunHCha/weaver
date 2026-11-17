import type { CSSProperties, RefCallback } from "react";
import type { Tag, Thread } from "../lib/api";
import {
  dateLabel,
  fieldText,
  kindClass,
  kindStyle,
  relations,
} from "../lib/presentation";
import { currentThread, isHistory } from "../lib/history";
import { Icon, References, Tags } from "./Common";
export interface CardProps {
  thread: Thread;
  threads: Thread[];
  tags: Tag[];
  selected: boolean;
  keep: boolean;
  neighbor: string | undefined;
  flashing: boolean;
  ghost?: boolean;
  mention?: boolean;
  style: CSSProperties;
  nodeRef: RefCallback<HTMLElement>;
  onHover: (id: string | null) => void;
  onPick: (id: string) => void;
}
export function ThreadCard({
  thread: t,
  threads,
  tags,
  selected,
  keep,
  neighbor,
  flashing,
  ghost = false,
  mention = false,
  style,
  nodeRef,
  onHover,
  onPick,
}: CardProps) {
  const state = relations(threads),
    field = fieldText(currentThread(t, threads, tags)),
    hollow = kindClass[t.kind] === "hol";
  return (
    <article
      ref={nodeRef}
      className={[
        "card",
        ghost ? "ghost" : "",
        isHistory(t) || state.retracted.has(t.thread_id) ? "history-item" : "",
        mention ? "mention-only" : "",
        "k-" + kindClass[t.kind],
        hollow ? "hol" : "",
        state.retracted.has(t.thread_id) ? "retracted" : "",
        selected ? "sel" : "",
        keep ? "keep" : "",
        neighbor === "직전" ? "nb-prev" : neighbor === "직후" ? "nb-next" : "",
        flashing ? "pulse" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-id={t.thread_id}
      data-nb={neighbor}
      aria-hidden={ghost || undefined}
      tabIndex={ghost ? -1 : 0}
      style={{ ...kindStyle(t.kind), ...style }}
      aria-label={`${t.kind} ${t.content || "거절"}`}
      onMouseEnter={() => onHover(t.thread_id)}
      onMouseLeave={() => onHover(null)}
      onClick={() => onPick(t.thread_id)}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          e.stopPropagation();
          onPick(t.thread_id);
        }
      }}
    >
      <div className="ctop">
        <span className="kw">{t.kind}</span>
        {mention && <span className="mention-mark">언급</span>}
        {isHistory(t) && <span className="mut">이력</span>}
        {state.isOpen(t) && <span className="op">열림</span>}
        <span className="time">{dateLabel(t.occurred_at)}</span>
      </div>
      <p className={"ctext" + (!t.content ? " nul" : "")}>
        {t.content || "거절됨 (내용 없음)"}
      </p>
      <div className="props">
        {t.kind === "요청" && state.resolved.has(t.thread_id) && (
          <span className="mark">
            <Icon name="resolves" size={11} />
            해결됨
          </span>
        )}
        {field && (
          <span className="chip">
            <b>{field[0]}</b>
            {field[1]}
          </span>
        )}
        {t.direction && (
          <span className="dir">
            <Icon name={t.direction === "받음" ? "in" : "out"} />
            {t.direction}
          </span>
        )}
      </div>
      <div className="cfoot">
        <span className="tags">
          <Tags thread={t} tags={tags} />
        </span>
        <References thread={t} threads={threads} />
      </div>
    </article>
  );
}
