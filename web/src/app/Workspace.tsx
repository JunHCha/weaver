"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type FabricData, type Stage, type Status } from "../lib/api";
import { DictionaryScreen } from "../components/DictionaryScreen";
import { AppShell } from "../components/AppShell";
import { FabricScreen } from "../components/FabricScreen";
import { OpsScreen } from "../components/OpsScreen";
import { useVisiblePolling } from "../hooks/visiblePolling";
import styles from "./Workspace.module.css";
export default function Workspace({
  screen = "fabric",
}: {
  screen?: "fabric" | "dictionary" | Stage;
}) {
  const editState = useRef({ busy: false, epoch: 0 });
  const [editingBusy, setEditingBusy] = useState(false);
  const onBusy = useCallback((busy: boolean) => {
    editState.current = { busy, epoch: editState.current.epoch + 1 };
    setEditingBusy(busy);
  }, []);
  const [data, setData] = useState<FabricData | null>(null),
    [error, setError] = useState(false),
    [retry, setRetry] = useState(0),
    [refreshedAt, setRefreshedAt] = useState<number | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setError(false);
    api
      .fabric(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setData(value);
          setRefreshedAt(Date.now());
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [retry]);
  const refresh = useCallback(async (signal: AbortSignal) => {
    if (editState.current.busy) return;
    const epoch = editState.current.epoch;
    try {
      const [threads, tags, states] = await Promise.all([
        api.threads(signal),
        api.tags(signal),
        api.states(signal),
      ]);
      if (
        signal.aborted ||
        editState.current.busy ||
        epoch !== editState.current.epoch
      )
        return;
      setData((old) => (old ? { ...old, threads, tags, states } : old));
      setRefreshedAt(Date.now());
    } catch {
      /* Keep the last successful snapshot and its refresh age. */
    }
  }, []);
  useVisiblePolling(
    refresh,
    30000,
    screen === "fabric" && data !== null && !editingBusy,
    false,
  );
  const onStatus = useCallback(
    (status: Status) => setData((old) => (old ? { ...old, status } : old)),
    [],
  );
  return (
    <div className={styles.workspace}>
      {error ? (
        <div className="empty" role="alert">
          서버에 연결할 수 없습니다.{" "}
          <code>uv run --project server python -m weaver.app web</code>으로
          서버를 시작하세요.
          <button type="button" onClick={() => setRetry(retry + 1)}>
            다시 시도
          </button>
        </div>
      ) : !data ? (
        <p role="status">불러오는 중…</p>
      ) : (
        <AppShell
          screen={screen}
          count={data.threads.length}
          waiting={data.status.loom.waiting_review}
        >
          {screen === "fabric" ? (
            <FabricScreen
              onBusy={onBusy}
              data={data}
              refreshedAt={refreshedAt}
              onSaved={() => void refresh(new AbortController().signal)}
            />
          ) : screen === "dictionary" ? (
            <DictionaryScreen
              onSaved={() => void refresh(new AbortController().signal)}
            />
          ) : (
            <OpsScreen
              key={screen}
              stage={screen}
              status={data.status}
              onStatus={onStatus}
            />
          )}
        </AppShell>
      )}
    </div>
  );
}
