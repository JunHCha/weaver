import { useEffect } from "react";
/** Each visibility session owns its timer, request cancellation and overlap guard. */
export function useVisiblePolling(
  load: (signal: AbortSignal) => Promise<void>,
  interval: number,
  enabled = true,
  immediate = true,
) {
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | undefined;
    let controller: AbortController | undefined;
    const stop = () => {
      clearInterval(timer);
      controller?.abort();
    };
    const start = (refresh: boolean) => {
      stop();
      if (document.hidden) return;
      const session = new AbortController();
      controller = session;
      let pending = false;
      const run = async () => {
        if (pending || session.signal.aborted || document.hidden) return;
        pending = true;
        try {
          await load(session.signal);
        } finally {
          pending = false;
        }
      };
      if (refresh) void run();
      timer = setInterval(() => void run(), interval);
    };
    const visibility = () => {
      if (document.hidden) stop();
      else start(true);
    };
    start(immediate);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [load, interval, enabled, immediate]);
}
