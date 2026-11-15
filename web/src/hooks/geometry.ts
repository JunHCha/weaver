import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import type { PlacedCard } from "../lib/fabric";

export function useCardGeometry(
  container: RefObject<HTMLDivElement | null>,
  nodes: RefObject<Map<string, HTMLElement>>,
  revision: string,
) {
  const [measurement, setMeasurement] = useState<{
    width: number;
    heights: Record<string, number>;
  }>({ width: 1200, heights: {} });
  useLayoutEffect(() => {
    let frame = 0;
    let active = true;
    const measure = () => {
      if (!active) return;
      const width = container.current?.clientWidth || 1200;
      const heights = Object.fromEntries(
        [...nodes.current].map(([key, node]) => [
          key,
          node.offsetHeight || 100,
        ]),
      );
      setMeasurement((old) =>
        old.width === width &&
        JSON.stringify(old.heights) === JSON.stringify(heights)
          ? old
          : { width, heights },
      );
    };
    const schedule = () => {
      if (!active) return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };
    const observer = new ResizeObserver(schedule);
    if (container.current) observer.observe(container.current);
    nodes.current.forEach((node) => observer.observe(node));
    measure();
    document.fonts?.ready.then(schedule);
    return () => {
      active = false;
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [container, nodes, revision]);
  return measurement;
}
export function useFlip(
  nodes: RefObject<Map<string, HTMLElement>>,
  cards: PlacedCard[],
  focus: string | null,
) {
  const previous = useRef(
    new Map<string, { x: number; y: number; width: number }>(),
  );
  const oldFocus = useRef(focus);
  const oldCards = useRef<PlacedCard[]>([]);
  const [exiting, setExiting] = useState<PlacedCard[]>([]);
  const exitTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  useEffect(() => () => clearTimeout(exitTimer.current), []);
  useLayoutEffect(() => {
    const changed = oldFocus.current !== focus;
    const reduce = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const next = new Map<string, { x: number; y: number; width: number }>();
    cards.forEach((card) => {
      next.set(card.key, { x: card.x, y: card.y, width: card.width });
      const node = nodes.current.get(card.key),
        old = previous.current.get(card.key);
      if (!node || !changed || reduce || !node.animate) return;
      if (old)
        node.animate(
          [
            { transform: `translate(${old.x - card.x}px,${old.y - card.y}px)` },
            { transform: "none" },
          ],
          { duration: 220, easing: "ease-out" },
        );
      else
        node.animate([{ opacity: 0 }, { opacity: 1 }], {
          duration: 160,
          delay: 80,
          fill: "backwards",
        });
    });
    if (changed) {
      clearTimeout(exitTimer.current);
      const keys = new Set(cards.map((card) => card.key));
      setExiting(
        reduce ? [] : oldCards.current.filter((card) => !keys.has(card.key)),
      );
      exitTimer.current = setTimeout(() => setExiting([]), 120);
    }
    oldCards.current = cards;
    previous.current = next;
    oldFocus.current = focus;
  }, [cards, focus, nodes]);
  return exiting;
}
