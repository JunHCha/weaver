import { useEffect, useRef } from "react";
import type { LogLine } from "../lib/api";
import { shortId } from "../lib/presentation";
import { Toggle } from "./Common";
export function LogTail({
  logs,
  source,
  level,
  follow,
  error,
  onLevel,
  onFollow,
}: {
  logs: LogLine[];
  source: string;
  level: string;
  follow: boolean;
  error: boolean;
  onLevel: (value: string) => void;
  onFollow: (value: boolean) => void;
}) {
  const tail = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (follow && tail.current)
      tail.current.scrollTop = tail.current.scrollHeight;
  }, [logs, follow]);
  return (
    <div className="logpane">
      <div className="logbar">
        <span className="ttl" id="lt2">
          {source
            ? `${shortId(source)} 로그 ${logs.length}줄`
            : `전체 로그 ${logs.length}줄`}
        </span>
        <span className="grow" />
        <div className="seg" role="group" aria-label="로그 수준">
          {["", "INFO", "WARNING"].map((value) => (
            <button
              type="button"
              key={value}
              data-l={value || "all"}
              aria-pressed={level === value}
              onClick={() => onLevel(value)}
            >
              {value || "전체"}
            </button>
          ))}
        </div>
        <Toggle
          id="fol"
          label="새 로그 따라가기"
          on={follow}
          onChange={() => onFollow(!follow)}
        />
      </div>
      <div
        className="logout"
        id="lo"
        ref={tail}
        tabIndex={0}
        aria-label="로그"
        onScroll={(e) => {
          const node = e.currentTarget;
          if (
            follow &&
            node.scrollHeight - node.scrollTop - node.clientHeight > 60
          )
            onFollow(false);
        }}
      >
        {error ? (
          <span role="alert">
            서버 연결을 확인하세요. python -m weaver.app web
          </span>
        ) : logs.length ? (
          logs.map((line, i) => (
            <span key={i} className="ll">
              <span className="tm">{line.ts}</span>{" "}
              <span className={"lv " + (line.lv === "WARNING" ? "w" : "i")}>
                {line.lv}
              </span>{" "}
              <span className="lg">{line.lg}:</span> {line.msg}
            </span>
          ))
        ) : (
          <span className="mut">조건에 맞는 로그가 없습니다.</span>
        )}
      </div>
    </div>
  );
}
