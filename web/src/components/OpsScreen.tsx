import { useState } from "react";
import type { Stage, Status } from "../lib/api";
import { mdhm } from "../lib/presentation";
import { useOperations } from "../hooks/operations";
import { SourceList } from "./SourceList";
import { LogTail } from "./LogTail";
export function OpsScreen({
  stage,
  status,
  onStatus,
}: {
  stage: Stage;
  status: Status;
  onStatus: (value: Status) => void;
}) {
  const [source, setSource] = useState(""),
    [level, setLevel] = useState(""),
    [follow, setFollow] = useState(true);
  const { sources, logs, error } = useOperations(
    stage,
    level,
    source,
    onStatus,
  );
  return (
    <section className="screen ops">
      <div className="pagehead">
        <h1>{stage === "spinner" ? "Spinner" : "Loom"}</h1>
      </div>
      <div className="lede">
        <i />
        마지막 활동{" "}
        {status[stage].last_activity
          ? mdhm(status[stage].last_activity)
          : "없음"}
        {stage === "loom" ? `, 검토 대기 ${status.loom.waiting_review}건` : ""}
      </div>
      <div className="opsbody">
        <SourceList sources={sources} selected={source} onSelect={setSource} />
        <LogTail
          logs={logs}
          source={source}
          level={level}
          follow={follow}
          error={error}
          onLevel={setLevel}
          onFollow={setFollow}
        />
      </div>
    </section>
  );
}
