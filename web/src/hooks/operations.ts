import { useEffect, useState } from "react";
import {
  api,
  type LogLine,
  type SourceView,
  type Stage,
  type Status,
} from "../lib/api";
export function useOperations(
  stage: Stage,
  level: string,
  source: string,
  onStatus: (value: Status) => void,
) {
  const [sources, setSources] = useState<SourceView[]>([]),
    [logs, setLogs] = useState<LogLine[]>([]),
    [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {
        const [list, lines, nextStatus] = await Promise.all([
          api.sources(stage, controller.signal),
          api.logs(stage, level, source, controller.signal),
          api.status(controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setSources(list);
        setLogs(lines);
        onStatus(nextStatus);
        setError(false);
      } catch {
        if (!controller.signal.aborted) setError(true);
      } finally {
        pending = false;
      }
    };
    load();
    const timer = setInterval(load, 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [stage, level, source, onStatus]);
  return { sources, logs, error };
}
