import { Fragment, useMemo, useRef, useState } from "react";
import type { Thread } from "../lib/api";
import {
  focusLayout,
  stateSummary,
  type Fact,
  type Lane,
  type TagState,
} from "../lib/fabric";
import { useCardGeometry, useFlip } from "../hooks/geometry";
import {
  distribute,
  isHistory,
  mentionMatch,
  type ThreadView,
  type ValueChange,
} from "../lib/history";
import { Icon } from "./Common";
import { ThreadCard } from "./ThreadCard";
import { WeftLayer } from "./WeftLayer";
export function CardBoard({
  changes,
  view,
  states,
  selectedTags = [],
  rows,
  lanes,
  groups,
  group,
  focus,
  selected,
  onPick,
  onSelect,
}: {
  changes: ValueChange[];
  view: ThreadView;
  states: Record<string, TagState>;
  selectedTags?: string[];
  rows: Thread[];

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
  const highlight = hover || selected,
    index = rows.findIndex((t) => t.thread_id === highlight);
  const keep = new Set(
    rows.slice(Math.max(0, index - 1), index + 2).map((t) => t.thread_id),
  );
  const relatedPeople = highlight ? view.peopleById.get(highlight) : undefined;
  if (relatedPeople)
    for (const name of relatedPeople)
      for (const id of view.personThreads.get(name) || []) keep.add(id);
  const rowMap = useMemo(
    () => new Map(rows.map((t) => [t.thread_id, t])),
    [rows],
  );
  const cardsByLane = useMemo(
    () => distribute(layout.cards, (card) => [String(card.lane)]),
    [layout.cards],
  );
  const changesByLane = useMemo(
    () =>
      group === "project" || group === "area"
        ? distribute(changes, ({ thread }) => groups(thread))
        : new Map<string, ValueChange[]>(),
    [changes, group, groups],
  );
  const boardHeight =
    layout.height +
    (group === "project" || group === "area" ? changes.length * 26 : 0);
  const nodeCallbacks = useRef(
    new Map<string, (node: HTMLElement | null) => void>(),
  );
  const nodeRef = (key: string) => {
    let callback = nodeCallbacks.current.get(key);
    if (!callback) {
      callback = (node) => {
        if (node) nodes.current.set(key, node);
        else nodes.current.delete(key);
      };
      nodeCallbacks.current.set(key, callback);
    }
    return callback;
  };
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
                {(group === "project" || group === "area") &&
                  !l.none &&
                  states[l.key] &&
                  !l.collapsed && (
                    <span className="lane-summary">
                      {stateSummary(states[l.key]).map(([name, value]) => (
                        <span key={name}>
                          {name} {value}
                        </span>
                      ))}
                    </span>
                  )}
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
          style={{ height: boardHeight }}
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
                  height: boardHeight,
                }}
              >
                <span>
                  {l.key} {collapsed.has(group) ? l.count : l.visible}
                </span>
              </div>
            ))}
          {layout.cards.map((card) => {
            const t = rowMap.get(card.id);
            if (!t) return null;
            return (
              <ThreadCard
                key={card.key}
                mention={mentionMatch(t, selectedTags, view.peopleTags)}
                thread={t}
                view={view}
                x={card.x}
                y={card.y}
                width={card.width}
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
                nodeRef={nodeRef(card.key)}
                onHover={setHover}
                onPick={onPick}
              />
            );
          })}
          {exiting.map((card) => {
            const t = view.byId.get(card.id);
            return t ? (
              <ThreadCard
                key={"ghost:" + card.key}
                ghost
                thread={t}
                view={view}
                selected={false}
                keep={false}
                neighbor={undefined}
                flashing={false}
                x={card.x}
                y={card.y}
                width={card.width}
                height={card.height}
                nodeRef={nodeRef("ghost:" + card.key)}
                onHover={() => {}}
                onPick={() => {}}
              />
            ) : null;
          })}
          {layout.lanes.flatMap((lane, laneIndex) => {
            if (lane.collapsed || (group !== "project" && group !== "area"))
              return [];
            const cards = (cardsByLane.get(String(laneIndex)) || []).filter(
              (card) => !isHistory(view.byId.get(card.id)!),
            );
            const laneChanges = changesByLane.get(lane.key) || [];
            // Both lists are chronological. Advance one cursor per lane.
            let cursor = 0;
            return laneChanges.map(({ thread, label }, i) => {
              while (
                cursor < cards.length &&
                Date.parse(rowMap.get(cards[cursor].id)!.occurred_at) <
                  Date.parse(thread.occurred_at)
              )
                cursor++;
              const next = cards[cursor];
              const y = next
                ? Math.max(0, next.y - 8)
                : (cards.at(-1)?.y || 0) + (cards.at(-1)?.height || 0) + 12;
              const dotY = y + i * 26;
              const anchorY = (next || cards.at(-1))?.y ?? 0;
              return (
                <Fragment key={`${lane.key}:${thread.thread_id}`}>
                  <span
                    aria-hidden="true"
                    className="history-spine"
                    style={{
                      left: lane.x - 7,
                      top: Math.min(anchorY + 18, dotY + 3),
                      height: Math.abs(dotY + 3 - anchorY - 18),
                    }}
                  />
                  <button
                    type="button"
                    className="history-dot"
                    aria-label={label}
                    title={label}
                    style={{ left: lane.x - 18, top: dotY - 8 }}
                    onClick={() => onSelect(thread.thread_id)}
                  />
                </Fragment>
              );
            });
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
