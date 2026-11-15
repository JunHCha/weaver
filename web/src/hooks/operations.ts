import { useCallback, useEffect, useState } from "react";
import {
  api,
  type LogLine,
  type SourceView,
  type Stage,
  type Status,
} from "../lib/api";
import { useVisiblePolling } from "./visiblePolling";
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
    setLogs([]);
    setError(false);
  }, [stage, level, source]);
  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const [list, lines, nextStatus] = await Promise.all([
          api.sources(stage, signal),
          api.logs(stage, level, source, signal),
          api.status(signal),
        ]);
        if (signal.aborted) return;
        setSources(list);
        setLogs(lines);
        onStatus(nextStatus);
        setError(false);
      } catch {
        if (!signal.aborted) setError(true);
      }
    },
    [stage, level, source, onStatus],
  );
  useVisiblePolling(load, 5000);
  return { sources, logs, error };
}
