import { useEffect, useState } from "react";
import { pulseDurations, type Point } from "../lib/fabric";
export interface CometAnchor {
  id: string;
  point: Point;
}
export interface CometFrame {
  path: string;
  distance: number;
  length: number;
  x: number;
  y: number;
}
function pulsePath(a: Point, b: Point): string {
  if (a.lane === b.lane)
    return `M${a.x} ${a.y}L${a.x - 6} ${a.y}L${b.x - 6} ${b.y}L${b.x} ${b.y}`;
  const d = Math.sign(b.x - a.x) || 1;
  return `M${a.x} ${a.y}C${a.x + d * 16} ${a.y} ${b.x - d * 16} ${b.y} ${b.x} ${b.y}`;
}
export function useComet(
  anchors: CometAnchor[],
  focus: string | null,
  onFlash: (ids: string[]) => void,
) {
  const [frame, setFrame] = useState<CometFrame | null>(null);
  useEffect(() => {
    let raf = 0,
      pause: ReturnType<typeof setTimeout> | undefined;
    const flashes = new Map<string, ReturnType<typeof setTimeout>>();
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const stop = () => {
      cancelAnimationFrame(raf);
      clearTimeout(pause);
      flashes.forEach((timer) => clearTimeout(timer));
      flashes.clear();
      setFrame(null);
      onFlash([]);
    };
    const run = () => {
      stop();
      if (!focus || document.hidden || media.matches) return;
      const start = anchors.findIndex((a) => a.id === focus);
      const route = start >= 0 ? anchors.slice(start, start + 31) : [];
      if (route.length < 2) return;
      const segments = route.slice(1).map((to, i) => {
        const path = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "path",
        );
        path.setAttribute("d", pulsePath(route[i].point, to.point));
        return { path, length: path.getTotalLength() };
      });
      const lengths = segments.map((s) => s.length),
        durations = pulseDurations(lengths);
      const starts = [0],
        cumulative = [0];
      durations.forEach((ms, i) => {
        starts.push(starts[i] + ms);
        cumulative.push(cumulative[i] + lengths[i]);
      });
      const joined = segments
        .map((s, i) =>
          i === 0
            ? s.path.getAttribute("d")
            : s.path.getAttribute("d")?.replace(/^M[^A-Za-z]+/, ""),
        )
        .join("");
      const total = cumulative[cumulative.length - 1],
        begin = performance.now();
      let reached = -1;
      const tick = (now: number) => {
        const elapsed = now - begin;
        let hop = 0;
        while (hop < durations.length - 1 && elapsed >= starts[hop + 1]) hop++;
        const fraction = Math.min(1, (elapsed - starts[hop]) / durations[hop]);
        const eased =
          fraction < 0.5
            ? 2 * fraction * fraction
            : 1 - Math.pow(-2 * fraction + 2, 2) / 2;
        const point = segments[hop].path.getPointAtLength(eased * lengths[hop]);
        const destination = hop + (fraction > 0.98 ? 1 : 0);
        if (destination !== reached) {
          reached = destination;
          const id = route[destination].id;
          clearTimeout(flashes.get(id));
          flashes.set(
            id,
            setTimeout(() => {
              flashes.delete(id);
              onFlash([...flashes.keys()]);
            }, 400),
          );
          onFlash([...flashes.keys()]);
        }
        if (elapsed >= starts[starts.length - 1]) {
          setFrame(null);
          pause = setTimeout(run, 1500);
          return;
        }
        setFrame({
          path: joined,
          distance: cumulative[hop] + eased * lengths[hop],
          length: total,
          x: point.x,
          y: point.y,
        });
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    };
    const restart = () => {
      stop();
      if (!document.hidden && !media.matches) pause = setTimeout(run, 350);
    };
    restart();
    document.addEventListener("visibilitychange", restart);
    media.addEventListener("change", restart);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", restart);
      media.removeEventListener("change", restart);
    };
  }, [anchors, focus, onFlash]);
  return frame;
}
