import { useMemo } from "react";
import type { Thread } from "../lib/api";
import {
  gapLabel,
  highlightedSegments,
  weftPath,
  type BoardLayout,
} from "../lib/fabric";
import { useComet } from "../hooks/comet";
export function WeftLayer({
  layout,
  rows,
  highlight,
  focus,
  onFlash,
  onPick,
}: {
  layout: BoardLayout;
  rows: Thread[];
  highlight: string | null;
  focus: string | null;
  onFlash: (ids: string[]) => void;
  onPick: (id: string) => void;
}) {
  const anchors = useMemo(
    () =>
      rows.flatMap((t) => {
        const copies = layout.cards
          .filter((c) => c.id === t.thread_id)
          .sort((a, b) => a.x - b.x);
        return copies.length
          ? [
              {
                id: t.thread_id,
                t,
                point: {
                  x: copies[0].x,
                  y: copies[0].y + 18,
                  lane: copies[0].lane,
                },
                others: copies
                  .slice(1)
                  .map((c) => ({ x: c.x, y: c.y + 18, lane: c.lane })),
              },
            ]
          : [];
      }),
    [layout.cards, rows],
  );
  const active = highlightedSegments(
    anchors.map((a) => a.id),
    highlight,
  );
  const pulse = useComet(anchors, focus, onFlash);
  const segments = anchors.slice(0, -1).map((a, i) => ({
    from: a,
    to: anchors[i + 1],
    path: weftPath(a.point, anchors[i + 1].point),
    highlighted: active.has(i),
  }));
  return (
    <>
      <svg
        className="svg-under"
        id="svg-u"
        width={layout.width}
        height={layout.height}
        aria-hidden="true"
      >
        {anchors
          .filter((a) => a.t.retracts)
          .map((a) => {
            const target = anchors.find((b) => b.id === a.t.retracts);
            return target ? (
              <path
                key={a.id}
                data-retraction={a.id}
                d={`M${target.point.x - 6} ${target.point.y}C${target.point.x - 26} ${target.point.y} ${a.point.x - 26} ${a.point.y} ${a.point.x - 6} ${a.point.y}`}
                fill="none"
                stroke="var(--muted)"
                strokeWidth="1.5"
                strokeDasharray="5 4"
              />
            ) : null;
          })}
        {segments.map((s, i) => (
          <path
            key={i}
            data-s={i}
            d={s.path}
            fill="none"
            stroke="var(--thread)"
            strokeWidth="1.25"
            strokeLinecap="round"
            className={"wf" + (s.highlighted ? " hl" : "")}
          />
        ))}
        {anchors.flatMap((a, i) =>
          a.others.map((p, j) => (
            <path
              key={`${i}:${j}`}
              data-o={i}
              className={"kn" + (highlight === a.id ? " hl" : "")}
              d={weftPath(a.point, p)}
              fill="none"
              stroke="var(--muted)"
              strokeWidth="1"
              strokeDasharray="3 3"
            />
          )),
        )}
      </svg>
      <svg
        className="svg-over"
        id="svg-o"
        width={layout.width}
        height={layout.height}
        aria-hidden="true"
      >
        {segments.map((s, i) => (
          <path
            key={i}
            data-s={i}
            d={s.path}
            fill="none"
            stroke="var(--thread)"
            className={"wf2" + (s.highlighted ? " hl" : "")}
          />
        ))}
        {anchors.map((a, i) => (
          <g key={a.id} data-o={i} className={highlight === a.id ? "hl" : ""}>
            <line
              x1={a.point.x - 6}
              y1={a.point.y}
              x2={a.point.x}
              y2={a.point.y}
              stroke="var(--thread)"
              strokeWidth="1.25"
            />
            <circle
              cx={a.point.x}
              cy={a.point.y}
              r="2.6"
              fill="var(--thread)"
            />
            {a.others.map((p, j) => (
              <circle
                key={j}
                cx={p.x}
                cy={p.y}
                r="2.4"
                fill="var(--bg)"
                stroke="var(--muted)"
                strokeWidth="1.25"
              />
            ))}
          </g>
        ))}
        {pulse && (
          <g id="pulse-g">
            {Array.from({ length: 8 }, (_, i) => {
              const hi = pulse.distance - i * 7.5,
                lo = Math.max(0, hi - 7.5),
                length = Math.max(0, hi - lo);
              return (
                <path
                  key={i}
                  d={pulse.path}
                  fill="none"
                  stroke="var(--thread)"
                  strokeWidth={Math.max(0.4, 3 * (1 - i / 8))}
                  strokeOpacity={1 - i / 8}
                  strokeDasharray={`${length} ${pulse.length * 2 + 10}`}
                  strokeDashoffset={-lo}
                />
              );
            })}
            <circle
              cx={pulse.x}
              cy={pulse.y}
              r="5"
              fill="var(--thread)"
              opacity=".25"
            />
            <circle cx={pulse.x} cy={pulse.y} r="3" fill="var(--thread)" />
          </g>
        )}
      </svg>
      {segments.map(
        (s, i) =>
          s.from.point.lane !== s.to.point.lane && (
            <button
              type="button"
              key={i}
              className={"elabel" + (s.highlighted ? " hl" : "")}
              data-s={i}
              tabIndex={s.highlighted ? 0 : -1}
              style={{
                left:
                  (s.to.point.x > s.from.point.x
                    ? s.to.point.x
                    : s.from.point.x) - 28,
                top:
                  s.to.point.x > s.from.point.x ? s.to.point.y : s.from.point.y,
              }}
              onClick={() => onPick(s.to.id)}
            >
              {gapLabel(s.from.t.occurred_at, s.to.t.occurred_at)}
            </button>
          ),
      )}
    </>
  );
}
