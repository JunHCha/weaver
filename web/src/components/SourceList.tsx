import type { SourceView } from "../lib/api";
import { mdhm, shortId } from "../lib/presentation";
const codes: Record<string, string> = {
  완료: "done",
  "처리 중": "run",
  "재시도 대기": "wait",
  중지됨: "stop",
  "검토 대기": "rev",
};
export function SourceList({
  sources,
  selected,
  onSelect,
}: {
  sources: SourceView[];
  selected: string;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="srclist" id="sl">
      <button
        type="button"
        className="srcitem"
        data-id=""
        aria-current={!selected ? "true" : undefined}
        onClick={() => onSelect("")}
      >
        <span className="a">
          <b style={{ fontWeight: 600 }}>전체 source</b>
        </span>
        <span className="b">{sources.length}개</span>
      </button>
      {sources.map((s) => (
        <button
          type="button"
          className="srcitem"
          key={s.id}
          data-id={s.id}
          aria-current={selected === s.id ? "true" : undefined}
          onClick={() => onSelect(s.id)}
        >
          <span className="a">
            <span className="mono">{shortId(s.id)}</span>
          </span>
          <span className="b">{mdhm(s.updated_at)}</span>
          <span className="c">
            <span>{s.kind}</span>
            <span className={"st " + codes[s.status]}>
              <i />
              {s.status}
            </span>
          </span>
        </button>
      ))}
    </div>
  );
}
