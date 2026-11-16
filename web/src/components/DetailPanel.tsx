import { TagEditor } from "./TagEditor";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import type { SourceView, Tag, Thread } from "../lib/api";
import {
  actorLabel,
  mdhm,
  fieldLabel,
  kindClass,
  kindStyle,
  refs,
  refLabels,
  incomingRefLabels,
  relations,
} from "../lib/presentation";
import { Icon, KindWord } from "./Common";
export function DetailPanel({
  thread: t,
  threads,
  sources,
  onClose,
  onPick,
  onSaved,
}: {
  thread: Thread | null;
  threads: Thread[];
  tags: Tag[];
  sources: SourceView[];
  onSaved?: () => void;
  onClose: () => void;
  onPick: (id: string) => void;
}) {
  const [toast, setToast] = useState("");
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 1600);
    return () => clearTimeout(timer);
  }, [toast]);
  if (!t)
    return <aside className="panel" id="panel" aria-label="Thread 상세" />;
  const relation = relations(threads),
    same = threads.filter((x) => x.source_id === t.source_id),
    source = sources.find((s) => s.id === t.source_id);
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setToast("복사했습니다");
    } catch {
      setToast("복사하지 못했습니다");
    }
  };
  const copyButton = (value: string) => (
    <button
      type="button"
      className="iconbtn"
      aria-label="복사"
      title="복사"
      data-copy={value}
      onClick={() => copy(value)}
    >
      <Icon name="copy" />
    </button>
  );
  const link = (id: string) => {
    const target = threads.find((x) => x.thread_id === id);
    return (
      <button
        type="button"
        className="linkbtn"
        onClick={() => onPick(id)}
        data-go={id}
      >
        seq {target?.seq ?? "?"} {target?.content || "거절"}
      </button>
    );
  };
  const delta = Math.round(
    (Date.parse(t.recorded_at) - Date.parse(t.occurred_at)) / 60000,
  );
  const rows: [string, ReactNode][] = [
    [
      "thread_id",
      <Fragment key="thread">
        <span className="v mono">{t.thread_id}</span>
        {copyButton(t.thread_id)}
      </Fragment>,
    ],
    ["seq", t.seq],
    ["유형", t.kind],
    ["발생 시각", t.occurred_at],
    [
      "기록 시각",
      <span key="recorded" className="v">
        {t.recorded_at}
        <br />
        <span className="mut">
          {delta < 60
            ? `${delta}분 뒤 기록`
            : `${(delta / 60).toFixed(delta % 60 ? 1 : 0)}시간 뒤 기록`}
        </span>
      </span>,
    ],
    [
      "태그",
      t.kind === "실행" ? (
        "—"
      ) : (
        <TagEditor key={t.thread_id} thread={t} onSaved={onSaved} />
      ),
    ],
    ["필드", t.field ? fieldLabel[t.field] : "—"],
    ["방향", t.direction || "—"],
    ["행위자", actorLabel[t.actor]],
    [
      "출처",
      <Fragment key="source">
        <span className="v mono">{t.source_id}</span>
        {copyButton(t.source_id)}
      </Fragment>,
    ],
  ];
  if (t.correction) rows.push(["태그 정정", JSON.stringify(t.correction)]);
  const outbound = refs.filter((key) => t[key]),
    inbound = threads.flatMap((from) =>
      refs
        .filter((key) => from[key] === t.thread_id)
        .map((key) => ({ from, key })),
    );
  return (
    <aside className="panel open" id="panel" aria-label="Thread 상세">
      <div className="ph">
        <KindWord thread={t} />
        {relation.isOpen(t) && <span className="mut">열린 요청</span>}
        {t.kind === "요청" && relation.resolved.has(t.thread_id) && (
          <span className="mark">해결됨</span>
        )}
        {relation.retracted.has(t.thread_id) && (
          <span className="mut">철회됨</span>
        )}
        <span className="grow" />
        <span className="mut">seq {t.seq}</span>
        <button
          id="pclose"
          className="iconbtn"
          type="button"
          title="닫기 (Esc)"
          aria-label="닫기"
          onClick={onClose}
        >
          <Icon name="x" size={14} />
        </button>
      </div>
      <div className="pb">
        <p className="pcontent">
          {t.content || <span className="mut">내용 없음 (거절)</span>}
        </p>
        <dl>
          {rows.map(([name, value]) => (
            <Fragment key={name}>
              <dt>{name}</dt>
              <dd>{value}</dd>
            </Fragment>
          ))}
        </dl>
        <div className="sec tag-history">
          <h3>태그 변경 이력</h3>
          {threads.filter((row) => row.correction?.target === t.thread_id)
            .length ? (
            <ul aria-label="태그 변경 이력">
              {threads
                .filter((row) => row.correction?.target === t.thread_id)
                .map((row) => (
                  <li key={row.thread_id}>
                    <time dateTime={row.recorded_at}>
                      {mdhm(row.recorded_at)}
                    </time>
                    <span>
                      {row.correction?.add.map((name) => (
                        <span className="tag-added" key={name}>
                          +{name}{" "}
                        </span>
                      ))}
                      {row.correction?.remove.map((name) => (
                        <span className="mut" key={name}>
                          −{name}{" "}
                        </span>
                      ))}
                    </span>
                    <span className="mut">
                      {actorLabel[row.actor]}
                      {relation.retracted.has(row.thread_id) ? " · 철회됨" : ""}
                    </span>
                  </li>
                ))}
            </ul>
          ) : (
            <span className="mut">태그 변경 이력이 없습니다</span>
          )}
        </div>
        <div className="sec">
          <h3>값</h3>
          <pre className="json">
            {t.value === null ? "—" : JSON.stringify(t.value, null, 2)}
          </pre>
        </div>
        <div className="sec">
          <h3>참조</h3>
          {outbound.length || inbound.length || t.correction ? (
            <div className="refs">
              {t.correction && (
                <div className="refrow">
                  <span className="lbl">정정 대상</span>
                  {link(t.correction.target)}
                </div>
              )}
              {outbound.map((key) => (
                <div className="refrow" key={key}>
                  <span className="lbl">
                    <Icon name={key} />
                    {refLabels[key]}
                  </span>
                  {link(t[key]!)}
                </div>
              ))}
              {inbound.map(({ from, key }) => (
                <div className="refrow" key={from.thread_id + key}>
                  <span className="lbl">
                    <Icon name={key} />
                    {incomingRefLabels[key]}
                  </span>
                  {link(from.thread_id)}
                </div>
              ))}
            </div>
          ) : (
            <span className="mut">연결된 Thread가 없습니다</span>
          )}
        </div>
        <div className="sec">
          <h3>
            같은 출처{" "}
            <span className="mut" style={{ fontWeight: 400, fontSize: 13 }}>
              {source?.kind || "출처"}{" "}
              {(source?.updated_at || t.occurred_at)
                .slice(5, 10)
                .replace("-", "/")}
              , {same.length}개
            </span>
          </h3>
          <div className="samelist">
            {same.map((x) => (
              <button
                type="button"
                key={x.thread_id}
                data-go={x.thread_id}
                className={x.thread_id === t.thread_id ? "cur" : ""}
                onClick={() => onPick(x.thread_id)}
              >
                <span
                  className={
                    "kcell " + (kindClass[x.kind] === "hol" ? "hollow" : "")
                  }
                  style={kindStyle(x.kind)}
                >
                  <i className="kdot" />
                  {x.kind}
                </span>
                <span className="t">{x.content || "거절"}</span>
                <span className="mut">{x.seq}</span>
              </button>
            ))}
          </div>
        </div>
        {toast && (
          <div role="status" className="toast">
            {toast}
          </div>
        )}
      </div>
    </aside>
  );
}
