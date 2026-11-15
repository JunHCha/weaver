import { useMemo, useRef, useState } from "react";
import type { Tag, Thread } from "../lib/api";
import { focusLayout, type Fact, type Lane } from "../lib/fabric";
import { useCardGeometry, useFlip } from "../hooks/geometry";
import { Icon } from "./Common";
import { ThreadCard } from "./ThreadCard";
import { WeftLayer } from "./WeftLayer";
export function CardBoard({
  rows,
  threads,
  tags,
  lanes,
  groups,
  group,
  focus,
  selected,
  onPick,
  onSelect,
}: {
  rows: Thread[];
  threads: Thread[];
  tags: Tag[];
  lanes: Lane[];
  groups: (t: Fact) => string[];
  group: string;
  focus: string | null;
  selected: string | null;
  onPick: (id: string) => void;
  onSelect: (id: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null),
    nodes = useRef(new Map<string, HTMLElement>());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set()),
    [hover, setHover] = useState<string | null>(null),
    [flash, setFlash] = useState<string[]>([]);
  const revision =
    rows.map((t) => t.thread_id).join("|") +
    group +
    focus +
    String(collapsed.has(group));
  const measured = useCardGeometry(container, nodes, revision);
  const layout = useMemo(
    () =>
      focusLayout(
        rows,
        lanes,
        groups,
        focus,
        collapsed.has(group),
        measured.width,
        measured.heights,
        group === "none",
      ),
    [rows, lanes, groups, focus, collapsed, group, measured],
  );
  const exiting = useFlip(nodes, layout.cards, focus);
  const highlight = hover || (!focus ? selected : null),
    index = rows.findIndex((t) => t.thread_id === highlight);
  const keep = new Set(
    rows.slice(Math.max(0, index - 1), index + 2).map((t) => t.thread_id),
  );
  return (
    <div className="scroller" id="scr" ref={container}>
      <div className="cwrap" id="cw" style={{ width: layout.width }}>
        {group !== "none" ? (
          <div
            className="chead"
            id="chead"
            style={{ width: layout.width, paddingLeft: 24, columnGap: 56 }}
          >
            {layout.lanes.map((l) => (
              <div
                key={l.key}
                className={"lane-h" + (l.collapsed ? " col" : "")}
                style={{ width: l.width }}
                title={l.key}
              >
                <b>{l.key}</b>
                <span>{l.count}</span>
                {l.none && (
                  <button
                    type="button"
                    className="iconbtn fold"
                    style={{
                      marginLeft: "auto",
                      transform: collapsed.has(group)
                        ? "rotate(-90deg)"
                        : "rotate(90deg)",
                    }}
                    aria-label={`${l.key} 접기/펼치기`}
                    onClick={() =>
                      setCollapsed((old) => {
                        const next = new Set(old);
                        if (next.has(group)) next.delete(group);
                        else next.add(group);
                        return next;
                      })
                    }
                  >
                    <Icon name="chev" />
                  </button>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div style={{ height: 12 }} />
        )}
        <div
          className={"cgrid" + (highlight ? " dimming" : "")}
          id="cg"
          style={{ height: layout.height }}
        >
          {group !== "none" &&
            layout.lanes.map((l) => (
              <div
                key={l.key}
                className={"lane-bg" + (l.collapsed ? " col" : "")}
                style={{
                  left: l.x,
                  width: l.width,
                  top: 0,
                  height: layout.height,
                }}
              >
                <span>
                  {l.key} {collapsed.has(group) ? l.count : l.visible}
                </span>
              </div>
            ))}
          {layout.cards.map((card) => {
            const t = rows.find((t) => t.thread_id === card.id);
            if (!t) return null;
            return (
              <ThreadCard
                key={card.key}
                thread={t}
                threads={threads}
                tags={tags}
                style={{ left: card.x, top: card.y, width: card.width }}
                selected={selected === card.id}
                keep={index >= 0 && keep.has(card.id)}
                neighbor={
                  index >= 0 && rows[index - 1]?.thread_id === card.id
                    ? "직전"
                    : index >= 0 && rows[index + 1]?.thread_id === card.id
                      ? "직후"
                      : undefined
                }
                flashing={flash.includes(card.id)}
                nodeRef={(node) => {
                  if (node) nodes.current.set(card.key, node);
                  else nodes.current.delete(card.key);
                }}
                onHover={setHover}
                onPick={onPick}
              />
            );
          })}
          {exiting.map((card) => {
            const t = threads.find((t) => t.thread_id === card.id);
            return t ? (
              <ThreadCard
                key={"ghost:" + card.key}
                ghost
                thread={t}
                threads={threads}
                tags={tags}
                selected={false}
                keep={false}
                neighbor={undefined}
                flashing={false}
                style={{
                  left: card.x,
                  top: card.y,
                  width: card.width,
                  height: card.height,
                }}
                nodeRef={() => {}}
                onHover={() => {}}
                onPick={() => {}}
              />
            ) : null;
          })}
          <WeftLayer
            layout={layout}
            rows={rows}
            highlight={highlight}
            focus={focus}
            onFlash={setFlash}
            onPick={onSelect}
          />
        </div>
      </div>
    </div>
  );
}
