import { useState, type ReactNode } from "react";
import type { Tag } from "../lib/api";
import { kinds } from "../lib/presentation";
import { tagKinds } from "../lib/fabric";
import { Icon, Toggle } from "./Common";
export interface Filters {
  view: "card" | "log";
  group: string;
  kinds: string[];
  tags: string[];
  period: "all" | "week" | "7d";
  openOnly: boolean;
  executions: boolean;
  query: string;
}
export const initialFilters: Filters = {
  view: "card",
  group: "none",
  kinds: [],
  tags: [],
  period: "all",
  openOnly: false,
  executions: false,
  query: "",
};
function Menu({
  label,
  id,
  children,
  active = false,
}: {
  label: string;
  id: string;
  children: ReactNode;
  active?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span
      style={{ position: "relative" }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          setOpen(false);
          e.stopPropagation();
        }
      }}
    >
      <button
        id={id}
        type="button"
        className="chipbtn"
        data-on={active}
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {label}
        <Icon name="chev" size={11} />
      </button>
      {open && (
        <div
          className="pop"
          role="group"
          onChange={() => setOpen(false)}
          onClick={(event) => {
            if (
              event.target instanceof HTMLElement &&
              event.target.closest(".foot button")
            )
              setOpen(false);
          }}
        >
          {children}
        </div>
      )}
    </span>
  );
}
export function FabricToolbar({
  filters: f,
  tags,
  onChange,
}: {
  filters: Filters;
  tags: Tag[];
  onChange: (value: Filters) => void;
}) {
  const toggle = (values: string[], value: string) =>
    values.includes(value)
      ? values.filter((v) => v !== value)
      : [...values, value];
  return (
    <div className="toolbar" id="tb">
      <div className="seg" role="group" aria-label="보기">
        {(["card", "log"] as const).map((view) => (
          <button
            key={view}
            type="button"
            data-v={view}
            aria-pressed={f.view === view}
            onClick={() => onChange({ ...f, view })}
          >
            {view === "card" ? "카드" : "로그"}
          </button>
        ))}
      </div>
      <label className="field">
        묶기{" "}
        <select
          id="grp"
          aria-label="묶기"
          value={f.group}
          onChange={(e) => onChange({ ...f, group: e.target.value })}
        >
          {[
            ["none", "없음"],
            ...Object.entries(tagKinds),
            ["kind", "유형"],
            ["source", "출처"],
          ].map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <Menu
        id="b-kind"
        label={`유형${f.kinds.length ? " " + f.kinds.length : ""}`}
        active={f.kinds.length > 0}
      >
        {kinds.map((kind) => (
          <label key={kind}>
            <input
              type="checkbox"
              checked={f.kinds.includes(kind)}
              onChange={() =>
                onChange({
                  ...f,
                  kinds: toggle(f.kinds, kind),
                  executions: kind === "실행" ? true : f.executions,
                })
              }
            />
            {kind}
          </label>
        ))}
        <div className="foot">
          <button onClick={() => onChange({ ...f, kinds: [] })}>
            선택 해제
          </button>
        </div>
      </Menu>
      <Menu
        id="b-tag"
        label={`태그${f.tags.length ? " " + f.tags.length : ""}`}
        active={f.tags.length > 0}
      >
        {[...Object.keys(tagKinds), "unknown"].map((kind) => (
          <div key={kind}>
            {tags.some((t) => (t.kind || "unknown") === kind) && (
              <div className="ph2">{tagKinds[kind] || "종류 미정"}</div>
            )}
            {tags
              .filter((t) => (t.kind || "unknown") === kind)
              .map((tag) => (
                <label key={tag.name}>
                  <input
                    type="checkbox"
                    checked={f.tags.includes(tag.name)}
                    onChange={() =>
                      onChange({ ...f, tags: toggle(f.tags, tag.name) })
                    }
                  />
                  {tag.name}
                </label>
              ))}
          </div>
        ))}
        <div className="foot">
          <button onClick={() => onChange({ ...f, tags: [] })}>
            선택 해제
          </button>
        </div>
      </Menu>
      <Menu
        id="b-per"
        label={`기간 ${f.period === "all" ? "전체" : f.period === "week" ? "이번 주" : "지난 7일"}`}
        active={f.period !== "all"}
      >
        {(
          [
            ["all", "전체"],
            ["week", "이번 주"],
            ["7d", "지난 7일"],
          ] as const
        ).map(([value, label]) => (
          <label key={value}>
            <input
              type="radio"
              name="period"
              checked={f.period === value}
              onChange={() => onChange({ ...f, period: value })}
            />
            {label}
          </label>
        ))}
      </Menu>
      <Toggle
        id="t-open"
        label="열린 요청만"
        on={f.openOnly}
        onChange={() => onChange({ ...f, openOnly: !f.openOnly })}
      />
      <Toggle
        id="t-exec"
        label="실행 기록 포함"
        on={f.executions}
        onChange={() => onChange({ ...f, executions: !f.executions })}
      />
      <label className="search">
        <span className="sr">내용 검색</span>
        <Icon name="search" size={13} />
        <input
          id="q"
          type="search"
          placeholder="내용 검색"
          autoComplete="off"
          value={f.query}
          onChange={(e) => onChange({ ...f, query: e.target.value })}
        />
      </label>
    </div>
  );
}
