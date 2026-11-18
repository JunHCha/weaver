import { TagEditor } from "./TagEditor";
import { useTagEditing } from "./TagEditing";
import { memo, type RefCallback } from "react";
import type { Thread } from "../lib/api";
import { dateLabel, kindClass, kindStyle } from "../lib/presentation";
import { isHistory, type ThreadView } from "../lib/history";
import { Icon, References, Tags } from "./Common";
export interface CardProps {
  thread: Thread;
  view: ThreadView;
  selected: boolean;
  keep: boolean;
  neighbor: string | undefined;
  flashing: boolean;
  ghost?: boolean;
  mention?: boolean;
  x: number;
  y: number;
  width: number;
  height?: number;
  nodeRef: RefCallback<HTMLElement>;
  onHover: (id: string | null) => void;
  onPick: (id: string) => void;
}
export const ThreadCard = memo(function ThreadCard({
  thread: t,
  view,
  selected,
  keep,
  neighbor,
  flashing,
  ghost = false,
  mention = false,
  x,
  y,
  width,
  height,
  nodeRef,
  onHover,
  onPick,
}: CardProps) {
  const editing = useTagEditing();
  const state = view.relation,
    field = view.fields.get(t.thread_id),
    hollow = kindClass[t.kind] === "hol",
    history = isHistory(t) || state.retracted.has(t.thread_id);
  return (
    <article
      ref={nodeRef}
      className={[
        "card",
        ghost ? "ghost" : "",
        history ? "history-item" : "",
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
      style={{ ...kindStyle(t.kind), left: x, top: y, width, height }}
      aria-label={`${t.kind} ${t.content || "거절"}${mention ? " · 언급" : ""}${isHistory(t) || state.retracted.has(t.thread_id) ? " · 이력" : ""}`}
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
        {history && <span className="history-type">이력</span>}
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
        <div className="tags">
          {editing && !ghost && t.kind !== "실행" ? (
            <TagEditor thread={t} compact />
          ) : (
            <Tags thread={t} knownTags={view.knownTags} />
          )}
        </div>
        <References thread={t} byId={view.byId} />
      </div>
    </article>
  );
});
