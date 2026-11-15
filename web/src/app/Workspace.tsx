"use client";
import { useEffect, useRef } from "react";
import styles from "./Workspace.module.css";
export default function Workspace() {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false;
    let cleanup: (() => void) | undefined;
    import("../lib/renderer").then(async ({ start }) => {
      if (!disposed && host.current) cleanup = await start(host.current);
      if (disposed) cleanup?.();
    });
    return () => {
      disposed = true;
      cleanup?.();
    };
  }, []);
  return (
    <div className={styles.workspace} ref={host}>
      <p role="status">불러오는 중…</p>
    </div>
  );
}
