import {
  groupKeys,
  tagKinds,
  selectFocus,
  packColumns,
  weftPath,
  gapLabel,
  pulseDurations,
  stateSummary as summarizeState,
} from "./fabric";

// The approved prototype's DOM renderer. Kept vanilla to preserve FLIP and SVG geometry.
export async function start(host) {
  const abort = new AbortController();
  const listen = (target, type, handler, options) =>
    target.addEventListener(
      type,
      handler,
      typeof options === "boolean"
        ? { capture: options, signal: abort.signal }
        : { ...options, signal: abort.signal },
    );
  const api = async (path) => {
    const r = await fetch("/api" + path, { signal: abort.signal });
    if (!r.ok) throw Error("API " + r.status);
    return r.json();
  };
  let threads,
    tags,
    states,
    spinSources,
    loomSources,
    spinLogs,
    loomLogs,
    status;
  try {
    [
      threads,
      tags,
      states,
      spinSources,
      loomSources,
      spinLogs,
      loomLogs,
      status,
    ] = await Promise.all([
      api("/threads?include_executions=true"),
      api("/tags"),
      api("/states"),
      api("/sources?stage=spinner"),
      api("/sources?stage=loom"),
      api("/logs?component=spinner"),
      api("/logs?component=loom"),
      api("/status"),
    ]);
  } catch {
    host.innerHTML =
      '<div class="empty" role="alert">서버에 연결할 수 없습니다. <code>uv run --project server python -m weaver.app web</code>으로 서버를 시작하세요.<button type="button">다시 시도</button></div>';
    host.querySelector("button").onclick = () => location.reload();
    return () => abort.abort();
  }
  host.innerHTML =
    '<div class="app">\n  <aside class="rail">\n    <div class="brand">\n      <img src="/logo.svg" width="28" height="28" alt="">\n      <span>weaver</span>\n    </div>\n    <nav class="nav" id="nav"></nav>\n    <div class="grow"></div>\n  </aside>\n  <main id="main"></main>\n</div>';

  /* ---------- helpers ---------- */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const el = (html) => {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  };
  const esc = (s) =>
    String(s ?? "").replace(
      /[&<>"]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
    );
  const IC = {
    in: '<path d="M9 3L3 9M3 4.5V9H7.5"/>',
    out: '<path d="M3 9L9 3M4.5 3H9V7.5"/>',
    resolves: '<path d="M2 6.5L5 9.5L10 3"/>',
    retracts: '<path d="M4 3L2 5L4 7M2 5H7A2.6 2.6 0 0 1 7 10.2H5"/>',
    updates: '<path d="M10 6A4 4 0 1 1 8.6 2.9M10 2V4.6H7.4"/>',
    blocked_by: '<path d="M3 5.5h6V10H3zM4.5 5.5V4a1.5 1.5 0 0 1 3 0v1.5"/>',
    executes: '<path d="M6.6 1.5L3 6.6H6L5.4 10.5L9 5.4H6z"/>',
    copy: '<rect x="4" y="4" width="6" height="6.5" rx="1"/><path d="M8 2.5H3.5A1 1 0 0 0 2.5 3.5V8"/>',
    chev: '<path d="M3 4.5L6 7.5L9 4.5"/>',
    x: '<path d="M3 3L9 9M9 3L3 9"/>',
    search: '<circle cx="5.2" cy="5.2" r="3.2"/><path d="M7.6 7.6L10.5 10.5"/>',
  };
  const ic = (n, s = 12) =>
    `<svg width="${s}" height="${s}" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[n]}</svg>`;

  const KINDS = ["요청", "결정", "일정", "기록", "태그정정", "거절", "실행"];
  const KCLS = {
    요청: "req",
    결정: "dec",
    일정: "sch",
    기록: "rec",
    태그정정: "hol",
    거절: "hol",
    실행: "hol",
  };
  const FIELD = {
    deadline: "마감",
    schedule: "일정",
    assignees: "담당자",
    status: "상태",
  };
  const ACTOR = {
    ai_approved: "AI 승인",
    human: "직접 작성",
    system: "시스템",
  };
  const TZ = "Asia/Seoul";
  const TAGKIND = Object.fromEntries(tags.map((t) => [t.name, t.kind]));
  const GKINDS = tagKinds;
  const SOURCES = [
    ...new Map(
      [...spinSources, ...loomSources].map((s) => [
        s.id,
        { ...s, day: s.updated_at.slice(5, 10) },
      ]),
    ).values(),
  ];
  for (const t of threads)
    if (!SOURCES.some((s) => s.id === t.source_id))
      SOURCES.push({
        id: t.source_id,
        kind: "출처",
        day: t.occurred_at.slice(5, 10),
      });
  const DATA = (() => {
    const list = threads;
    const byId = Object.fromEntries(list.map((t) => [t.thread_id, t]));
    const REFS = ["resolves", "retracts", "updates", "blocked_by", "executes"];
    const refsIn = {},
      resolvedBy = {},
      retractedBy = {};
    const retracted = new Set(list.map((t) => t.retracts).filter(Boolean));
    list.forEach((t) =>
      REFS.forEach((k) => {
        if (t[k]) {
          (refsIn[t[k]] ||= []).push({ from: t, kind: k });
          if (k === "resolves" && !retracted.has(t.thread_id) && !t.retracts)
            (resolvedBy[t[k]] ||= []).push(t);
          if (k === "retracts") (retractedBy[t[k]] ||= []).push(t);
        }
      }),
    );
    return { list, byId, REFS, refsIn, resolvedBy, retractedBy };
  })();
  const srcById = Object.fromEntries(SOURCES.map((s) => [s.id, s]));
  const srcLabel = (id) =>
    `${srcById[id].kind} ${srcById[id].day.replace("-", "/")}`;
  const shortId = (id) =>
    id.length > 18 ? id.slice(0, 11) + "…" + id.slice(-4) : id;
  const WD = "일월화수목금토";
  const dayKey = (iso) => iso.slice(0, 10);
  const dayLabel = (iso) =>
    `${+iso.slice(5, 7)}월 ${+iso.slice(8, 10)}일 ${WD[new Date(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10))).getUTCDay()]}`;
  const dayShort = (iso) => `${+iso.slice(5, 7)}/${+iso.slice(8, 10)}`;
  const hm = (iso) => iso.slice(11, 16);
  const mdhm = (iso) => `${iso.slice(5, 7)}/${iso.slice(8, 10)} ${hm(iso)}`;
  function fieldText(t) {
    if (!t.field || t.value === null) return null;
    const v = t.value;
    if (t.field === "deadline") return ["마감", dayShort(v)];
    if (t.field === "schedule")
      return [
        "일정",
        `${dayShort(v.start)} ${hm(v.start)}–${v.end ? hm(v.end) : "—"}`,
      ];
    if (t.field === "assignees")
      return [
        "담당자",
        [
          ...(v.add || []).map((n) => "+" + n),
          ...(v.remove || []).map((n) => "−" + n),
        ].join(", "),
      ];
    return ["상태", v];
  }
  const isResolved = (t) => !!DATA.resolvedBy[t.thread_id];
  const isRetracted = (t) => !!DATA.retractedBy[t.thread_id];
  const isOpenReq = (t) =>
    t.kind === "요청" && !isResolved(t) && !isRetracted(t);

  /* ---------- state ---------- */
  const S = {
    focus: null,
    collapsed: new Set(),
    screen: "fabric",
    view: "card",
    group: "none",
    kinds: new Set(),
    tags: new Set(),
    period: "all",
    openOnly: false,
    exec: false,
    q: "",
    sel: null,
    panel: false,
  };
  const ALL_TAGS = [...new Set(DATA.list.flatMap((t) => t.tags))];
  const now = new Date();
  const weekStart = new Date(now);
  weekStart.setDate(now.getDate() - ((now.getDay() + 6) % 7));
  weekStart.setHours(0, 0, 0, 0);
  function visible() {
    return DATA.list.filter((t) => {
      if (!S.exec && t.kind === "실행") return false;
      if (S.kinds.size && !S.kinds.has(t.kind)) return false;
      if (S.tags.size && !t.tags.some((x) => S.tags.has(x))) return false;
      if (
        S.period === "week" &&
        Date.parse(t.occurred_at) < weekStart.getTime()
      )
        return false;
      if (
        S.period === "7d" &&
        Date.parse(t.occurred_at) < now.getTime() - 7 * 86400000
      )
        return false;
      if (S.openOnly && !isOpenReq(t)) return false;
      if (S.q) {
        const q = S.q.toLowerCase();
        if (!(
          (t.content || "").toLowerCase().includes(q) ||
          t.tags.some((x) => x.includes(q)) ||
          t.thread_id.includes(q)
        ))
          return false;
      }
      return true;
    });
  }
  const noneKey = () => GKINDS[S.group] + " 없음";
  function groupsOf(t) {
    return groupKeys(t, S.group, TAGKIND, srcLabel);
  }
  function lanesOf(vis) {
    const counts = new Map();
    vis.forEach((t) =>
      groupsOf(t).forEach((g) => counts.set(g, (counts.get(g) || 0) + 1)),
    );
    let keys = [...counts.keys()];
    if (GKINDS[S.group]) {
      const nk = noneKey();
      keys.sort(
        (a, b) =>
          (a === nk) - (b === nk) ||
          counts.get(b) - counts.get(a) ||
          a.localeCompare(b, "ko"),
      );
    } else if (S.group === "kind")
      keys.sort((a, b) => KINDS.indexOf(a) - KINDS.indexOf(b));
    return keys.map((k) => ({
      key: k,
      count: counts.get(k),
      none: GKINDS[S.group] ? k === noneKey() : false,
    }));
  }
  /* ---------- shell ---------- */
  const main = $("#main");
  function renderNav() {
    const items = [
      ["fabric", "Fabric", `Thread ${DATA.list.length}개`],
      ["spinner", "Spinner", ""],
      ["loom", "Loom", String(status.loom.waiting_review)],
    ];
    $("#nav").innerHTML = items
      .map(
        ([k, n, s]) =>
          `<button type="button" data-s="${k}" ${S.screen === k ? 'aria-current="page"' : ""}><span>${n}</span>${k === "loom" ? `<span class="badge" title="검토 대기 ${status.loom.waiting_review}건">${s}</span>` : `<span class="sub">${s}</span>`}</button>`,
      )
      .join("");
    $$("#nav button").forEach((b) => (b.onclick = () => go(b.dataset.s)));
  }
  function go(s) {
    location.href = s === "fabric" ? "/" : "/" + s;
  }

  /* ---------- Fabric ---------- */
  let ro = null,
    raf = 0,
    lastLayout = null;
  function buildFabric() {
    main.innerHTML = `<section class="screen"><div style="display:flex;flex:1;min-height:0;position:relative">
    <div class="fabric-main">
      <div class="pagehead"><h1>Fabric</h1><span class="count" id="cnt"></span></div>
      <div class="toolbar" id="tb">
        <div class="seg" role="group" aria-label="보기"><button type="button" data-v="card">카드</button><button type="button" data-v="log">로그</button></div>
        <label class="field">묶기 <select id="grp" aria-label="묶기"><option value="none">없음</option><option value="project">프로젝트</option><option value="person">사람</option><option value="organization">조직</option><option value="topic">주제</option><option value="kind">유형</option><option value="source">출처</option></select></label>
        <span style="position:relative"><button class="chipbtn" type="button" id="b-kind" aria-haspopup="true"></button></span>
        <span style="position:relative"><button class="chipbtn" type="button" id="b-tag" aria-haspopup="true"></button></span>
        <span style="position:relative"><button class="chipbtn" type="button" id="b-per" aria-haspopup="true"></button></span>
        <button class="toggle" type="button" id="t-open" aria-pressed="false"><i></i>열린 요청만</button>
        <button class="toggle" type="button" id="t-exec" aria-pressed="false"><i></i>실행 기록 포함</button>
        <label class="search"><span class="sr">내용 검색</span>${ic("search", 13)}<input id="q" type="search" placeholder="내용 검색" autocomplete="off"></label>
      </div>
      <div class="fbar" id="fbar" hidden></div>
      <div class="scroller" id="scr" tabindex="-1"></div>
    </div>
    <aside class="panel" id="panel" aria-label="Thread 상세"></aside>
  </div></section>`;
    $$("#tb .seg button").forEach(
      (b) =>
        (b.onclick = () => {
          S.view = b.dataset.v;
          renderContent();
        }),
    );
    $("#grp").onchange = (e) => {
      S.group = e.target.value;
      renderContent();
    };
    $("#q").oninput = (e) => {
      S.q = e.target.value.trim();
      renderContent();
    };
    $("#t-open").onclick = () => {
      S.openOnly = !S.openOnly;
      renderContent();
    };
    $("#t-exec").onclick = () => {
      S.exec = !S.exec;
      renderContent();
    };
    popFor(
      "#b-kind",
      () => KINDS.map((k) => ({ v: k, label: k, on: S.kinds.has(k) })),
      (v, on) => {
        if (on) S.kinds.add(v);
        else S.kinds.delete(v);
        if (on && v === "실행") S.exec = true;
        renderContent();
      },
      () => {
        S.kinds.clear();
        renderContent();
      },
    );
    popFor(
      "#b-tag",
      () =>
        [...Object.keys(GKINDS), null].flatMap((kd) => {
          const tg = ALL_TAGS.filter((x) => (TAGKIND[x] || null) === kd);
          return tg.length
            ? [
                { header: kd ? GKINDS[kd] : "종류 미정" },
                ...tg.map((k) => ({ v: k, label: k, on: S.tags.has(k) })),
              ]
            : [];
        }),
      (v, on) => {
        if (on) S.tags.add(v);
        else S.tags.delete(v);
        renderContent();
      },
      () => {
        S.tags.clear();
        renderContent();
      },
    );
    popFor(
      "#b-per",
      () =>
        [
          ["all", "전체"],
          ["week", "이번 주"],
          ["7d", "지난 7일"],
        ].map(([v, label]) => ({ v, label, on: S.period === v })),
      (v) => {
        S.period = v;
        renderContent();
        closePop();
      },
      null,
      true,
    );
    $("#grp").value = S.group;
    $("#q").value = S.q;
    $("#panel").onclick = null;
    if (ro) ro.disconnect();
    ro = new ResizeObserver(scheduleWeft);
    ro.observe($("#scr"));
    renderContent();
    if (S.panel && S.sel) openPanel(S.sel);
  }
  /* popovers */
  let curPop = null;
  function closePop() {
    if (curPop) {
      curPop.remove();
      curPop = null;
    }
  }
  listen(document, "mousedown", (e) => {
    if (curPop && !curPop.contains(e.target) && !e.target.closest(".chipbtn"))
      closePop();
  });
  function popFor(sel, items, onChange, onClear, radio) {
    const btn = $(sel);
    btn.onclick = () => {
      if (curPop && curPop._btn === btn) {
        closePop();
        return;
      }
      closePop();
      const p = el(`<div class="pop" role="group"></div>`);
      p._btn = btn;
      p.innerHTML =
        items()
          .map((it, i) =>
            it.header
              ? `<div class="ph2">${esc(it.header)}</div>`
              : `<label><input type="${radio ? "radio" : "checkbox"}" name="pp" data-i="${i}" ${it.on ? "checked" : ""}>${esc(it.label)}</label>`,
          )
          .join("") +
        (onClear
          ? `<div class="foot"><button type="button">선택 해제</button></div>`
          : "");
      const its = items();
      $$("input", p).forEach(
        (inp) =>
          (inp.onchange = () => onChange(its[+inp.dataset.i].v, inp.checked)),
      );
      if (onClear)
        $(".foot button", p).onclick = () => {
          onClear();
          $$("input", p).forEach((i) => (i.checked = false));
        };
      btn.parentElement.appendChild(p);
      curPop = p;
      $("input", p)?.focus();
    };
  }
  listen(
    document,
    "keydown",
    (e) => {
      if (e.key === "Escape" && curPop) {
        const b = curPop._btn;
        closePop();
        b.focus();
        e.stopPropagation();
      }
    },
    true,
  );
  function renderToolbar(vis) {
    $$("#tb .seg button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.v === S.view)),
    );
    const setChip = (id, label, n) => {
      const b = $(id);
      b.dataset.on = n ? "true" : "false";
      b.innerHTML = `${label}${n ? ` <span class="n">${n}</span>` : ""}${ic("chev", 11)}`;
    };
    setChip("#b-kind", "유형", S.kinds.size);
    setChip("#b-tag", "태그", S.tags.size);
    const b = $("#b-per");
    b.dataset.on = S.period !== "all" ? "true" : "false";
    b.innerHTML = `기간 ${S.period === "all" ? "전체" : S.period === "week" ? "이번 주" : "지난 7일"}${ic("chev", 11)}`;
    $("#t-open").setAttribute("aria-pressed", S.openOnly);
    $("#t-exec").setAttribute("aria-pressed", S.exec);
    $("#cnt").innerHTML = `Thread <b>${vis.length}</b>개`;
  }
  function resetFilters() {
    S.kinds.clear();
    S.tags.clear();
    S.period = "all";
    S.openOnly = false;
    S.exec = false;
    S.q = "";
    const q = $("#q");
    if (q) q.value = "";
  }
  let CUR = { vis: [], cards: new Map() };
  function renderContent() {
    closePop();
    const vis = visible();
    CUR.vis = vis;
    renderToolbar(vis);
    const scr = $("#scr");
    scr.innerHTML = "";
    CUR.cards = new Map();
    if (!vis.length) {
      S.focus = null;
      stopPulse();
      renderFocusBar();
      scr.innerHTML = `<div class="empty">조건에 맞는 Thread가 없습니다. 필터를 줄여 보세요.<button type="button" id="rst">필터 초기화</button></div>`;
      $("#rst").onclick = () => {
        resetFilters();
        renderContent();
      };
      return;
    }
    if (
      S.view !== "card" ||
      (S.focus && !vis.some((t) => t.thread_id === S.focus))
    ) {
      S.focus = null;
      stopPulse();
    }
    renderFocusBar();
    if (S.view === "card") renderCards(vis, scr);
    else renderLog(vis, scr);
    updateSelection(true);
    if (S.focus) startPulse();
    scheduleWeft();
  }
  function kindPill(t) {
    return `<span class="kind ${KCLS[t.kind] === "hol" ? "hollow" : ""}" style="--k:var(--c-${KCLS[t.kind]})"><i class="kdot"></i>${t.kind}</span>`;
  }
  function relIcons(t, withNum = true) {
    return DATA.REFS.filter((k) => t[k])
      .map((k) => {
        const tg = DATA.byId[t[k]] || { seq: "?" };
        const lab = {
          resolves: "해결",
          retracts: "철회",
          updates: "변경",
          blocked_by: "선행 요청",
          executes: "실행 대상",
        }[k];
        return `<span class="rel" title="${lab} seq ${tg.seq}" aria-label="${lab} seq ${tg.seq}">${ic(k)}${withNum ? tg.seq : ""}</span>`;
      })
      .join("");
  }
  const GAP = 10,
    LGAP = 56,
    ATT = 18,
    COLW = 44;
  const wdOf = (iso) =>
    WD[
      new Date(
        Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)),
      ).getUTCDay()
    ];
  const dtLabel = (iso) => `${dayShort(iso)} ${wdOf(iso)} ${hm(iso)}`;
  const UNK_TIP = "종류가 정해지지 않은 태그 — wiki에서 종류를 정하세요";
  const tagsHtml = (t) =>
    t.tags
      .map((x) =>
        TAGKIND[x]
          ? `<span class="tg">#${esc(x)}</span>`
          : `<span class="tg unk" title="${UNK_TIP}">#${esc(x)}</span>`,
      )
      .join(" ");
  function renderCards(vis, scr) {
    const lanes =
      S.group === "none" ? [{ key: "", count: vis.length }] : lanesOf(vis);
    lanes.forEach((l) => (l.user = !!l.none && S.collapsed.has(S.group)));
    const laneIdx = new Map(lanes.map((l, i) => [l.key, i]));
    const wrap = el(`<div class="cwrap" id="cw"></div>`);
    if (S.group !== "none") {
      const head = el(`<div class="chead" id="chead"></div>`);
      lanes.forEach((l) => {
        const h = el(
          `<div class="lane-h" title="${esc(l.key)}"><b>${esc(l.key)}</b><span>${l.count}</span>${l.none ? `<button class="iconbtn fold" type="button" aria-label="${esc(l.key)} 접기/펼치기" title="접기/펼치기" style="margin-left:auto">${ic("chev", 12)}</button>` : ""}</div>`,
        );
        const b = $("button", h);
        if (b) {
          b.onclick = () => {
            if (S.collapsed.has(S.group)) S.collapsed.delete(S.group);
            else S.collapsed.add(S.group);
            renderContent();
          };
          b.style.transform = l.user ? "rotate(-90deg)" : "rotate(90deg)";
        }
        head.appendChild(h);
      });
      wrap.appendChild(head);
    } else wrap.appendChild(el(`<div style="height:12px"></div>`));
    const body = el(`<div class="cgrid" id="cg"></div>`);
    const items = [];
    if (S.group !== "none")
      lanes.forEach((l) =>
        body.appendChild(el(`<div class="lane-bg"><span></span></div>`)),
      );
    vis.forEach((t) => {
      const gs = groupsOf(t)
        .map((g) => laneIdx.get(g))
        .sort((a, b) => a - b);
      const els = gs.map(() => {
        const c = makeCard(t);
        body.appendChild(c);
        return c;
      });
      CUR.cards.set(t.thread_id, els);
      items.push({ t, gs, els });
    });
    body.insertAdjacentHTML(
      "beforeend",
      `<svg class="svg-under" id="svg-u"></svg><svg class="svg-over" id="svg-o"></svg>`,
    );
    wrap.appendChild(body);
    scr.appendChild(wrap);
    CUR.layout = { items, lanes };
    CUR.hover = null;
    body.addEventListener("click", (e) => {
      const lb = e.target.closest(".elabel");
      if (lb) {
        if (lb.dataset.t) {
          CUR.hover = null;
          pick(lb.dataset.t, { scroll: true, open: S.panel });
          if (S.focus) setFocus(lb.dataset.t);
        }
        return;
      }
      const c = e.target.closest(".card");
      if (c) {
        pick(c.dataset.id, { open: true });
        setFocus(c.dataset.id);
      }
    });
    body.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.classList.contains("card")) {
        e.preventDefault();
        pick(e.target.dataset.id, { open: true });
        setFocus(e.target.dataset.id);
      }
    });
    let hoverT = 0;
    body.addEventListener("mouseover", (e) => {
      clearTimeout(hoverT);
      const c = e.target.closest(".card");
      if (c && c.dataset.id !== CUR.hover) {
        CUR.hover = c.dataset.id;
        applyFocus();
      }
    });
    body.addEventListener("mouseout", (e) => {
      const to = e.relatedTarget;
      if (to && to.closest && (to.closest(".elabel") || to.closest(".card")))
        return;
      clearTimeout(hoverT);
      hoverT = setTimeout(() => {
        CUR.hover = null;
        applyFocus();
      }, 220);
    });
  }
  function focusSet() {
    return S.view === "card" ? selectFocus(CUR.vis, S.focus) : null;
  }
  function layoutCards() {
    const L = CUR.layout,
      scr = $("#scr"),
      wrap = $("#cw"),
      body = $("#cg");
    if (!L || !wrap) return;
    const lanes = L.lanes,
      n = lanes.length,
      single = S.group === "none",
      cw = scr.clientWidth,
      padR = 24,
      fs = focusSet();
    lanes.forEach((l) => (l.n = 0));
    L.items.forEach((it) =>
      it.els.forEach((c, k) => {
        const ln = lanes[it.gs[k]];
        c._on = !ln.user && (!fs || fs.has(it.t.thread_id));
        if (c._on) ln.n++;
      }),
    );
    lanes.forEach((l) => (l.strip = l.user || (!!fs && l.n === 0)));
    const nCol = lanes.filter((l) => l.strip).length,
      nOpen = Math.max(1, n - nCol);
    let padL = 24,
      laneW;
    if (single) {
      laneW = Math.min(720, cw - 96);
      padL = Math.max(48, Math.floor((cw - laneW) / 2));
    } else
      laneW = Math.max(
        190,
        Math.min(
          300,
          Math.floor((cw - padL - padR - (n - 1) * LGAP - nCol * COLW) / nOpen),
        ),
      );
    const gapX = single ? 0 : LGAP;
    let x = padL;
    lanes.forEach((l) => {
      l.x = x;
      l.w = l.strip ? COLW : laneW;
      l.y = 4;
      x += l.w + gapX;
    });
    wrap.style.width = Math.max(x - gapX + padR, cw) + "px";
    const packingItems = L.items.map((it) => {
      const on = it.els.map((c, k) => {
        const ln = lanes[it.gs[k]];
        c.style.display = c._on ? "" : "none";
        c.style.width = ln.w + "px";
        c.style.height = "";
        return c._on ? c.offsetHeight : -GAP;
      });
      return { lanes: it.gs, heights: on };
    });
    const packed = packColumns(packingItems, n, GAP);
    L.items.forEach((it, i) =>
      it.els.forEach((c, k) => {
        if (!c._on) return;
        c.style.left = lanes[it.gs[k]].x + "px";
        c.style.top = packed.positions[i][k] + "px";
        c.style.height = packingItems[i].heights[k] + "px";
      }),
    );
    lanes.forEach((l, i) => (l.y = packed.bottoms[i]));
    const H = Math.max(...lanes.map((l) => l.y)) + 60;
    body.style.height = H + "px";
    if (!single)
      $$(".lane-bg", body).forEach((e, i) => {
        const l = lanes[i];
        e.style.left = l.x + "px";
        e.style.width = l.w + "px";
        e.style.top = "0";
        e.style.height = H + "px";
        e.classList.toggle("col", l.strip);
        $("span", e).textContent = `${l.key} ${l.user ? l.count : l.n}`;
      });
    const hd = $("#chead");
    if (hd) {
      hd.style.width = wrap.style.width;
      hd.style.paddingLeft = padL + "px";
      hd.style.columnGap = gapX + "px";
      $$(".lane-h", hd).forEach((h, i) => {
        h.style.width = lanes[i].w + "px";
        h.classList.toggle("col", lanes[i].strip);
      });
    }
  }
  /* ---- focus mode with FLIP ---- */
  const reduceMotion = () =>
    matchMedia("(prefers-reduced-motion: reduce)").matches;
  function renderFocusBar() {
    const bar = $("#fbar");
    if (!bar) return;
    const t = S.focus && S.view === "card" ? DATA.byId[S.focus] : null;
    if (!t) {
      bar.hidden = true;
      return;
    }
    const k = CUR.vis.findIndex((x) => x.thread_id === t.thread_id);
    bar.hidden = false;
    bar.innerHTML = `<span><b>${mdhm(t.occurred_at)} ${t.kind}</b>부터 보는 중 (이전 ${Math.min(4, k)}개 포함)</span><button type="button" id="fexit">전체 보기</button>`;
    $("#fexit").onclick = () => setFocus(null);
  }
  function setFocus(id) {
    if (S.view !== "card" || !CUR.layout) {
      S.focus = null;
      return;
    }
    if (id === S.focus) {
      if (id) startPulse();
      return;
    }
    const body = $("#cg"),
      scr = $("#scr"),
      reduce = reduceMotion();
    const first = new Map();
    CUR.layout.items.forEach((it) =>
      it.els.forEach((c) => {
        if (c._on && c.style.display !== "none")
          first.set(c, {
            x: c.offsetLeft,
            y: c.offsetTop,
            w: c.offsetWidth,
            h: c.offsetHeight,
          });
      }),
    );
    S.focus = id;
    renderFocusBar();
    stopPulse();
    drawWeft();
    scr.scrollTop = 0;
    if (!reduce) {
      CUR.layout.items.forEach((it) =>
        it.els.forEach((c) => {
          const f = first.get(c);
          if (c._on) {
            if (f) {
              const dx = f.x - c.offsetLeft,
                dy = f.y - c.offsetTop;
              if (dx || dy)
                c.animate(
                  [
                    { transform: `translate(${dx}px,${dy}px)` },
                    { transform: "none" },
                  ],
                  { duration: 220, easing: "ease-out" },
                );
            } else
              c.animate([{ opacity: 0 }, { opacity: 1 }], {
                duration: 160,
                delay: 80,
                fill: "backwards",
              });
          } else if (f) {
            const g = c.cloneNode(true);
            g.classList.add("ghost");
            g.classList.remove("sel");
            Object.assign(g.style, {
              display: "",
              left: f.x + "px",
              top: f.y + "px",
              width: f.w + "px",
              height: f.h + "px",
              pointerEvents: "none",
            });
            body.appendChild(g);
            g.animate([{ opacity: 1 }, { opacity: 0 }], {
              duration: 120,
              fill: "forwards",
            }).onfinish = () => g.remove();
          }
        }),
      );
      ["#svg-u", "#svg-o"].forEach((s) =>
        $(s).animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220 }),
      );
    }
    if (id) setTimeout(startPulse, 260);
  }
  /* ---- pulse ---- */
  const pulse = { first: 0, pause: 0, raf: 0 };
  const TK = (n) =>
    parseFloat(getComputedStyle(document.documentElement).getPropertyValue(n));
  function stopPulse() {
    clearTimeout(pulse.first);
    clearTimeout(pulse.pause);
    cancelAnimationFrame(pulse.raf);
    $("#pulse-g")?.remove();
    $$(".card.pulse").forEach((c) => c.classList.remove("pulse"));
  }
  function startPulse() {
    stopPulse();
    if (!S.focus || S.view !== "card" || document.hidden || reduceMotion())
      return;
    pulse.first = setTimeout(runPulse, 350);
  }
  listen(document, "visibilitychange", () => {
    if (document.hidden) stopPulse();
    else if (S.focus) startPulse();
  });
  function segD(p, q) {
    if (q.lane === p.lane)
      return `M${p.x} ${p.y}L${p.x - 6} ${p.y}L${q.x - 6} ${q.y}L${q.x} ${q.y}`;
    const dir = Math.sign(q.x - p.x) || 1;
    return `M${p.x} ${p.y}C${p.x + dir * 16} ${p.y} ${q.x - dir * 16} ${q.y} ${q.x} ${q.y}`;
  }
  const easeIO = (f) => (f < 0.5 ? 2 * f * f : 1 - Math.pow(-2 * f + 2, 2) / 2);
  function runPulse() {
    if (!S.focus || document.hidden || reduceMotion()) return;
    cancelAnimationFrame(pulse.raf);
    $("#pulse-g")?.remove();
    $$(".card.pulse").forEach((c) => c.classList.remove("pulse"));
    const P = CUR.pts?.slice(0, Math.max(0, CUR.order.indexOf(S.sel)) + 31),
      su = $("#svg-o");
    if (!P || !su || !S.sel || !S.focus) return;
    const k = CUR.order.indexOf(S.sel);
    const again = () => {
      pulse.pause = setTimeout(runPulse, TK("--dur-pulse-pause"));
    };
    if (k < 0 || k >= P.length - 1) {
      again();
      return;
    }
    const NS = "http://www.w3.org/2000/svg",
      tmp = document.createElementNS(NS, "path");
    let d = "",
      cum = [0],
      lens = [];
    for (let i = k; i < P.length - 1; i++) {
      const sd = segD(P[i].p, P[i + 1].p);
      tmp.setAttribute("d", sd);
      const len = tmp.getTotalLength();
      lens.push(len);
      cum.push(cum[cum.length - 1] + len);
      d += i === k ? sd : sd.replace(/^M[^A-Za-z]+/, "");
    }
    const total = cum[cum.length - 1],
      hops = lens.length;
    const flashMs = TK("--dur-pulse-flash");
    const hopMs = pulseDurations(lens);
    const start = [0];
    hopMs.forEach((m) => start.push(start[start.length - 1] + m));
    const T = start[hops];
    const g = document.createElementNS(NS, "g");
    g.id = "pulse-g";
    const tail = TK("--sz-pulse-tail"),
      N = 8,
      seg = tail / N,
      w0 = TK("--sw-pulse");
    const tails = [];
    for (let i = 0; i < N; i++) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", "none");
      p.setAttribute("stroke", "var(--thread)");
      p.setAttribute("stroke-width", Math.max(0.4, w0 * (1 - i / N)));
      p.setAttribute("stroke-opacity", 1 - i / N);
      p.setAttribute("stroke-linecap", "butt");
      p.setAttribute("stroke-dasharray", `0 ${total * 2 + 10}`);
      g.appendChild(p);
      tails.push(p);
    }
    const mkc = (r, o) => {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("r", r);
      c.setAttribute("fill", "var(--thread)");
      c.setAttribute("opacity", o);
      g.appendChild(c);
      return c;
    };
    const glow = mkc(TK("--sw-pulse-glow") / 2, TK("--op-pulse-glow")),
      head = mkc(TK("--sw-pulse-head") / 2, 1);
    su.appendChild(g);
    const t0 = performance.now(),
      flashed = new Set();
    const flash = (j) => {
      const c = P[j].el;
      c.classList.add("pulse");
      setTimeout(() => c.classList.remove("pulse"), flashMs);
    };
    const finish = () => {
      g.remove();
      again();
    };
    const place = (len) => {
      tails.forEach((p, i) => {
        const hi = len - i * seg,
          lo = Math.max(0, hi - seg),
          l = hi - lo;
        if (l <= 0) {
          p.setAttribute("stroke-dasharray", `0 ${total * 2 + 10}`);
          return;
        }
        p.setAttribute("stroke-dasharray", `${l} ${total * 2 + 10}`);
        p.setAttribute("stroke-dashoffset", -lo);
      });
      const pt = tails[0].getPointAtLength(Math.min(len, total));
      [glow, head].forEach((c) => {
        c.setAttribute("cx", pt.x);
        c.setAttribute("cy", pt.y);
      });
    };
    flash(k);
    flashed.add(k);
    const frame = (now) => {
      if (!g.isConnected) {
        again();
        return;
      }
      const e = now - t0;
      if (e >= T) {
        for (let j = k; j < P.length; j++)
          if (!flashed.has(j)) {
            flashed.add(j);
            flash(j);
          }
        finish();
        return;
      }
      let i = 0;
      while (i < hops - 1 && e >= start[i + 1]) i++;
      const f = easeIO((e - start[i]) / hopMs[i]);
      place(cum[i] + f * lens[i]);
      for (let j = k; j <= k + i + (f > 0.98 ? 1 : 0); j++)
        if (!flashed.has(j)) {
          flashed.add(j);
          flash(j);
        }
      pulse.raf = requestAnimationFrame(frame);
    };
    pulse.raf = requestAnimationFrame(frame);
  }
  function makeCard(t) {
    const ft = fieldText(t),
      hol = KCLS[t.kind] === "hol";
    const cls = ["card", "k-" + KCLS[t.kind]];
    if (hol) cls.push("hol");
    if (isRetracted(t)) cls.push("retracted");
    return el(`<article class="${cls.join(" ")}" data-id="${esc(t.thread_id)}" tabindex="0" style="--k:var(--c-${KCLS[t.kind]})" aria-label="${t.kind} ${esc(t.content || "거절")}">
    <div class="ctop"><span class="kw">${t.kind}</span>${isOpenReq(t) ? '<span class="op">열림</span>' : ""}<span class="time">${dtLabel(t.occurred_at)}</span></div>
    <p class="ctext ${t.content ? "" : "nul"}">${t.content ? esc(t.content) : "거절됨 (내용 없음)"}</p>
    <div class="props">${t.kind === "요청" && isResolved(t) ? `<span class="mark">${ic("resolves", 11)}해결됨</span>` : ""}${ft ? `<span class="chip"><b>${ft[0]}</b>${esc(ft[1])}</span>` : ""}${t.direction ? `<span class="dir">${ic(t.direction === "받음" ? "in" : "out")}${t.direction}</span>` : ""}</div>
    <div class="cfoot"><span class="tags">${tagsHtml(t)}</span><span class="rels">${relIcons(t)}</span></div>
  </article>`);
  }
  /* ---- log: sections + reference-chain graph ---- */
  function stateSummary(rows, key) {
    const state =
      states[key] ||
      (() => {
        const state = {
          deadline: null,
          schedule: null,
          status: null,
          assignees: [],
          open_requests: [],
        };
        const assignees = new Set();
        DATA.list
          .filter((t) => groupsOf(t).includes(key))
          .forEach((t) => {
            if (
              isRetracted(t) ||
              t.retracts ||
              ["태그정정", "거절", "실행"].includes(t.kind)
            )
              return;
            if (isOpenReq(t)) state.open_requests.push(t.thread_id);
            if (t.field === "assignees") {
              if (t.value === null) assignees.clear();
              else {
                (t.value.add || []).forEach((n) => assignees.add(n));
                (t.value.remove || []).forEach((n) => assignees.delete(n));
              }
            } else if (t.field) state[t.field] = t.value;
          });
        state.assignees = [...assignees];
        return state;
      })();
    return summarizeState(state)
      .map(([k, v]) => `<span class="sm"><b>${esc(k)}</b> ${esc(v)}</span>`)
      .join("");
  }
  function renderLog(vis, scr) {
    const groups =
      S.group === "none"
        ? [{ key: "", rows: vis }]
        : lanesOf(vis).map((l) => ({
            key: l.key,
            rows: vis.filter((t) => groupsOf(t).includes(l.key)),
          }));
    const wrap = el(`<div class="ltable" id="lt" style="--gw:36px"></div>`);
    wrap.appendChild(
      el(
        `<div class="lrow lhead"><div></div><div>시각</div><div>유형</div><div>내용</div><div>필드/값</div><div>방향</div><div>태그</div><div>행위자</div><div>참조</div><div class="r">seq</div></div>`,
      ),
    );
    const grouped = S.group !== "none",
      hh = grouped ? 34 : 0;
    groups.forEach((g) => {
      const sec = el(`<div class="lsec"></div>`);
      if (grouped)
        sec.appendChild(
          el(
            `<div class="lsech"><b>${esc(g.key)}</b><span class="mut">${g.rows.length}</span><span class="sums">${stateSummary(g.rows, g.key)}</span></div>`,
          ),
        );
      g.rows.forEach((t) => {
        const ft = fieldText(t);
        const hol = KCLS[t.kind] === "hol";
        const r =
          el(`<div class="lrow k-${KCLS[t.kind]} ${hol ? "hol" : ""} ${isRetracted(t) ? "retracted" : ""}" data-id="${esc(t.thread_id)}" tabindex="0" style="--k:var(--c-${KCLS[t.kind]})">
        <div></div><div class="mut">${mdhm(t.occurred_at)}</div><div><span class="kw">${t.kind}</span></div>
        <div class="lc" title="${esc(t.content || "")}">${t.content ? esc(t.content) : '<span class="mut">내용 없음</span>'}${t.kind === "요청" && isResolved(t) ? ' <span class="mark">해결됨</span>' : ""}</div>
        <div>${ft ? `<span class="mut">${ft[0]}</span> ${esc(ft[1])}` : '<span class="mut">—</span>'}</div>
        <div class="mut">${t.direction ? `<span class="dir">${ic(t.direction === "받음" ? "in" : "out")}${t.direction}</span>` : ""}</div>
        <div class="mut">${tagsHtml(t)}</div>
        <div>${ACTOR[t.actor]}</div><div class="mut"><span class="rels">${relIcons(t, false)}</span></div><div class="r mut">${t.seq}</div></div>`);
        sec.appendChild(r);
        (
          CUR.cards.get(t.thread_id) ||
          CUR.cards.set(t.thread_id, []).get(t.thread_id)
        ).push(r);
      });
      const Y = (i) => hh + i * 34 + 17,
        n = g.rows.length;
      let sv =
        n > 1
          ? `<line x1="14" x2="14" y1="${Y(0)}" y2="${Y(n - 1)}" stroke="var(--thread)" stroke-width="1.25" opacity=".55"/>`
          : "";
      g.rows.forEach((t, i) => {
        const hol = KCLS[t.kind] === "hol",
          c = `var(--c-${KCLS[t.kind]})`;
        sv += `<circle data-id="${esc(t.thread_id)}" cx="14" cy="${Y(i)}" r="3.6" fill="${hol ? "var(--bg)" : c}" stroke="${hol ? c : "var(--bg)"}" stroke-width="${hol ? 1.5 : 1.5}"><title>seq ${t.seq} ${t.kind}</title></circle>`;
      });
      const svg = el(
        `<svg width="36" height="${hh + n * 34}" class="lsvg">${sv}</svg>`,
      );
      sec.appendChild(svg);
      wrap.appendChild(sec);
    });
    scr.appendChild(wrap);
    wrap.addEventListener("click", (e) => {
      const c = e.target.closest("[data-id]");
      if (c) pick(c.dataset.id, { open: true });
    });
    wrap.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.dataset?.id) {
        e.preventDefault();
        pick(e.target.dataset.id, { open: true });
      }
    });
  }
  const idx0 = (rows, t) => rows.indexOf(t);
  /* ---- card view: one chronological weft ---- */
  function gapTxt(a, b) {
    return gapLabel(a.occurred_at, b.occurred_at);
  }
  function scheduleWeft() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(drawWeft);
  }
  function drawWeft() {
    if (S.screen !== "fabric" || S.view !== "card") return;
    const body = $("#cg");
    if (!body) return;
    layoutCards();
    const W = body.offsetWidth,
      H = body.offsetHeight,
      su = $("#svg-u"),
      so = $("#svg-o");
    [su, so].forEach((s) => {
      s.setAttribute("width", W);
      s.setAttribute("height", H);
      s.setAttribute("viewBox", `0 0 ${W} ${H}`);
    });
    $$(".elabel", body).forEach((n) => n.remove());
    const L = CUR.layout,
      byT = new Map(L.items.map((it) => [it.t.thread_id, it]));
    // anchor = leftmost visible copy
    const order = [];
    CUR.vis.forEach((t) => {
      const it = byT.get(t.thread_id);
      if (!it) return;
      const vis = it.els
        .map((e, k) => ({ e, lane: it.gs[k] }))
        .filter((c) => c.e._on);
      if (!vis.length) return;
      const pt = (c) => ({
        x: c.e.offsetLeft,
        y: c.e.offsetTop + ATT,
        lane: c.lane,
      });
      order.push({
        t,
        p: pt(vis[0]),
        el: vis[0].e,
        others: vis.slice(1).map(pt),
      });
    });
    CUR.order = order.map((o) => o.t.thread_id);
    CUR.pts = order;
    const curve = (a, b) => {
      const dir = Math.sign(b.x - a.x) || 1;
      return `M${a.x} ${a.y}C${a.x + dir * 16} ${a.y} ${b.x - dir * 16} ${b.y} ${b.x} ${b.y}`;
    };
    let under = "",
      over = "",
      labels = "";
    const SW = TK("--sw-weft-idle"),
      SWK = TK("--sw-knot");
    order.forEach((o, i) => {
      const { p } = o;
      over += `<g data-o="${i}"><line x1="${p.x - 6}" y1="${p.y}" x2="${p.x}" y2="${p.y}" stroke="var(--thread)" stroke-width="${SW}"/><circle cx="${p.x}" cy="${p.y}" r="2.6" fill="var(--thread)"/></g>`;
      o.others.forEach((q) => {
        under += `<path data-o="${i}" d="${weftPath(p, q)}" fill="none" stroke="var(--muted)" stroke-width="${SWK}" stroke-dasharray="3 3" class="kn"/>`;
        over += `<circle data-o="${i}" cx="${q.x}" cy="${q.y}" r="2.4" fill="var(--bg)" stroke="var(--muted)" stroke-width="1.25"/>`;
      });
      if (i + 1 < order.length) {
        const n = order[i + 1],
          q = n.p;
        if (q.lane === p.lane) {
          const dd = weftPath(p, q);
          under += `<path data-s="${i}" d="${dd}" fill="none" stroke="var(--thread)" stroke-width="${SW}" stroke-linecap="round" class="wf"/>`;
          over += `<path data-s="${i}" d="${dd}" fill="none" stroke="var(--thread)" class="wf2" stroke-linecap="round"/>`;
        } else {
          under += `<path data-s="${i}" d="${weftPath(p, q)}" fill="none" stroke="var(--thread)" stroke-width="${SW}" class="wf"/>`;
          over += `<path data-s="${i}" d="${weftPath(p, q)}" fill="none" stroke="var(--thread)" class="wf2"/>`;
          const lx = q.x > p.x ? q.x - 28 : p.x - 28,
            ly = q.x > p.x ? q.y : p.y;
          labels += `<div class="elabel" data-s="${i}" style="left:${lx}px;top:${ly}px">${gapTxt(o.t, n.t)}</div>`;
        }
      }
    });
    su.innerHTML = under;
    so.innerHTML = over;
    body.insertAdjacentHTML("beforeend", labels);
    applyFocus();
  }
  function applyFocus() {
    const fid = S.view === "card" ? CUR.hover || S.sel : null;
    const order = CUR.order || [],
      k = fid ? order.indexOf(fid) : -1;
    const body = $("#cg");
    const dim = !!fid && !!(CUR.hover || (S.sel && !S.focus));
    body?.classList.toggle("dimming", dim);
    $$("[data-s]").forEach((n) => {
      const s = +n.dataset.s,
        on = k >= 0 && s >= k - 2 && s <= k + 1;
      n.classList.toggle("hl", on);
      if (n.classList.contains("elabel"))
        n.dataset.t = on ? order[s < k ? s : s + 1] || "" : "";
    });
    $$("[data-o]").forEach((n) => {
      const o = +n.dataset.o;
      n.classList.toggle("hl", k >= 0 && o >= k - 2 && o <= k + 2);
    });
    $$(".card.nb-prev,.card.nb-next").forEach((c) => {
      c.classList.remove("nb-prev", "nb-next");
      delete c.dataset.nb;
    });
    $$(".card.keep").forEach((c) => c.classList.remove("keep"));
    if (fid) {
      const i = CUR.vis.findIndex((t) => t.thread_id === fid);
      (CUR.cards.get(fid) || []).forEach((c) => c.classList.add("keep"));
      [
        [i - 1, "nb-prev", "직전"],
        [i + 1, "nb-next", "직후"],
      ].forEach(([j, cl, tx]) => {
        const t = CUR.vis[j];
        if (t)
          (CUR.cards.get(t.thread_id) || []).forEach((c) => {
            c.classList.add(cl, "keep");
            c.dataset.nb = tx;
          });
      });
    }
  }
  function drawArcs() {}
  listen(window, "resize", scheduleWeft);
  if (document.fonts) document.fonts.ready.then(scheduleWeft);

  /* selection / panel */
  function pick(id, o = {}) {
    const t = DATA.byId[id];
    if (!t) return;
    if (!CUR.cards.has(id)) {
      resetFilters();
      S.exec = true;
      renderContent();
      showToast("필터에 가려진 Thread라 필터를 초기화했습니다");
    }
    S.sel = id;
    if (o.open) S.panel = true;
    if (S.focus && !selectFocus(CUR.vis, S.focus)?.has(id)) setFocus(id);
    updateSelection(false);
    if (S.view === "card") layoutCards();
    if (o.scroll) {
      const e = CUR.cards.get(id)?.[0];
      if (e)
        e.scrollIntoView({
          block: "center",
          behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
            ? "auto"
            : "smooth",
        });
    }
    if (o.focus) CUR.cards.get(id)?.[0]?.focus({ preventScroll: true });
    if (S.panel) openPanel(id);
    scheduleWeft();
  }
  function updateSelection() {
    $$(".card.sel,.lrow.sel").forEach((c) => c.classList.remove("sel"));
    if (S.sel)
      (CUR.cards.get(S.sel) || []).forEach((c) => c.classList.add("sel"));
    if (!S.panel) $("#panel")?.classList.remove("open");
    applyFocus();
  }
  function copyBtn(v) {
    return `<button class="iconbtn" type="button" data-copy="${esc(v)}" title="복사" aria-label="복사">${ic("copy")}</button>`;
  }
  function openPanel(id) {
    const t = DATA.byId[id],
      p = $("#panel");
    if (!t) return;
    const was = p.classList.contains("open");
    p.classList.add("open");
    $(".fabric-main").classList.add("pnl");
    if (was) p.style.animation = "none";
    else p.style.animation = "";
    const delta = Math.round(
      (new Date(t.recorded_at) - new Date(t.occurred_at)) / 60000,
    );
    const deltaTxt =
      delta < 60
        ? `${delta}분 뒤 기록`
        : `${(delta / 60).toFixed(delta % 60 ? 1 : 0)}시간 뒤 기록`;
    const L = {
      resolves: "해결한 요청",
      retracts: "철회한 Thread",
      updates: "변경한 일정",
      blocked_by: "선행 요청",
      executes: "실행 대상",
    };
    const IN = {
      resolves: "이 요청을 해결함",
      retracts: "이 Thread를 철회함",
      updates: "이 일정을 변경함",
      blocked_by: "이 요청을 기다림",
      executes: "이 Thread를 실행함",
    };
    const refLink = (x) => {
      const r = DATA.byId[x];
      return `<button type="button" class="linkbtn" data-go="${esc(x)}" title="${esc(r?.content || "")}">seq ${r?.seq ?? "?"} ${esc(r?.content || "거절")}</button>`;
    };
    const correctionRef = t.correction
      ? `<div class="refrow"><span class="lbl">정정 대상</span>${refLink(t.correction.target)}</div>`
      : "";
    const outs = DATA.REFS.filter((k) => t[k])
      .map(
        (k) =>
          `<div class="refrow"><span class="lbl">${ic(k)}${L[k]}</span>${refLink(t[k])}</div>`,
      )
      .join("");
    const ins = (DATA.refsIn[id] || [])
      .map(
        (r) =>
          `<div class="refrow"><span class="lbl">${ic(r.kind)}${IN[r.kind]}</span>${refLink(r.from.thread_id)}</div>`,
      )
      .join("");
    const same = DATA.list.filter((x) => x.source_id === t.source_id);
    const src = srcById[t.source_id];
    const val = t.value === null ? "—" : esc(JSON.stringify(t.value, null, 2));
    const row = (k, v) => `<dt>${k}</dt><dd>${v}</dd>`;
    const nul = '<span class="mut">—</span>';
    p.innerHTML = `<div class="ph">${kindPill(t)}${isOpenReq(t) ? '<span class="mut">열린 요청</span>' : t.kind === "요청" && isResolved(t) ? '<span class="mark">해결됨</span>' : ""}${isRetracted(t) ? '<span class="mut">철회됨</span>' : ""}<span class="grow"></span><span class="mut">seq ${t.seq}</span><button class="iconbtn" type="button" id="pclose" title="닫기 (Esc)" aria-label="닫기">${ic("x", 14)}</button></div>
  <div class="pb">
    <p class="pcontent">${t.content ? esc(t.content) : '<span class="mut">내용 없음 (거절)</span>'}</p>
    <dl>
      ${row("thread_id", `<span class="v mono">${esc(t.thread_id)}</span>${copyBtn(t.thread_id)}`)}
      ${row("seq", t.seq)}${t.correction ? row("태그 정정", esc(JSON.stringify(t.correction))) : ""}
      ${row("유형", t.kind)}
      ${row("발생 시각", `<span class="v">${t.occurred_at}</span>`)}
      ${row("기록 시각", `<span class="v">${t.recorded_at}<br><span class="mut">${deltaTxt}</span></span>`)}
      ${row("태그", t.tags.length ? tagsHtml(t) : nul)}
      ${row("필드", t.field ? FIELD[t.field] : nul)}
      ${row("방향", t.direction || nul)}
      ${row("행위자", ACTOR[t.actor])}
      ${row("출처", `<span class="v mono">${esc(t.source_id)}</span>${copyBtn(t.source_id)}`)}
    </dl>
    <div class="sec"><h3>값</h3><pre class="json">${val}</pre></div>
    <div class="sec"><h3>참조</h3>${outs || ins || correctionRef ? `<div class="refs">${correctionRef}${outs}${ins}</div>` : '<span class="mut">연결된 Thread가 없습니다</span>'}</div>
    <div class="sec"><h3>같은 출처 <span class="mut" style="font-weight:400;font-size:13px">${src.kind} ${src.day.replace("-", "/")}, ${same.length}개</span></h3>
      <div class="samelist">${same.map((x) => `<button type="button" data-go="${esc(x.thread_id)}" class="${x.thread_id === id ? "cur" : ""}"><span class="kcell ${KCLS[x.kind] === "hol" ? "hollow" : ""}" style="--k:var(--c-${KCLS[x.kind]})"><i class="kdot"></i>${x.kind}</span><span class="t">${esc(x.content || "거절")}</span><span class="mut">${x.seq}</span></button>`).join("")}</div></div>
  </div>`;
    $("#pclose").onclick = closePanel;
    $$("[data-go]", p).forEach(
      (b) =>
        (b.onclick = () => pick(b.dataset.go, { open: true, scroll: true })),
    );
    $$("[data-copy]", p).forEach(
      (b) =>
        (b.onclick = async () => {
          try {
            await navigator.clipboard.writeText(b.dataset.copy);
            showToast("복사했습니다");
          } catch {
            showToast("복사하지 못했습니다");
          }
        }),
    );
  }
  function closePanel() {
    S.panel = false;
    $("#panel")?.classList.remove("open");
    $(".fabric-main")?.classList.remove("pnl");
    drawArcs();
    scheduleWeft();
  }
  let toastT;
  function showToast(m) {
    document.querySelector(".toast")?.remove();
    const t = el(`<div class="toast" role="status">${m}</div>`);
    document.body.appendChild(t);
    clearTimeout(toastT);
    toastT = setTimeout(() => t.remove(), 1600);
  }
  listen(document, "keydown", (e) => {
    if (S.screen !== "fabric") return;
    const tag = e.target.tagName;
    if (e.key === "Escape" && S.focus) {
      setFocus(null);
      return;
    }
    if (e.key === "Escape" && S.panel) {
      closePanel();
      return;
    }
    if (e.key === "Escape" && S.sel) {
      S.sel = null;
      updateSelection();
      return;
    }
    if (
      tag === "INPUT" ||
      tag === "SELECT" ||
      e.metaKey ||
      e.ctrlKey ||
      e.altKey
    )
      return;
    if (e.key === "j" || e.key === "k") {
      const vis = CUR.vis;
      if (!vis.length) return;
      let i = vis.findIndex((t) => t.thread_id === S.sel);
      i =
        e.key === "j"
          ? Math.min(vis.length - 1, i + 1)
          : Math.max(0, i < 0 ? 0 : i - 1);
      pick(vis[i].thread_id, { scroll: true, focus: true });
      e.preventDefault();
    } else if (
      e.key === "Enter" &&
      S.sel &&
      !e.target.dataset?.id &&
      tag !== "BUTTON"
    ) {
      pick(S.sel, { open: true });
    }
  });

  /* ---------- Spinner / Loom ---------- */
  const statusCode = {
    완료: "done",
    "처리 중": "run",
    "재시도 대기": "wait",
    중지됨: "stop",
    "검토 대기": "rev",
  };
  const makeOps = (stage, list, logs) => ({
    title: stage === "spinner" ? "Spinner" : "Loom",
    lede: `마지막 활동 ${status[stage].last_activity ? mdhm(status[stage].last_activity) : "없음"}${stage === "loom" ? ", 검토 대기 " + status.loom.waiting_review + "건" : ""}`,
    list: list.map((s) => ({
      ...s,
      st: statusCode[s.status],
      at: mdhm(s.updated_at),
    })),
    logs,
  });
  const OPS = {
    spinner: makeOps("spinner", spinSources, spinLogs),
    loom: makeOps("loom", loomSources, loomLogs),
  };
  const ST = {
    done: "완료",
    run: "처리 중",
    wait: "재시도 대기",
    stop: "중지됨",
    rev: "검토 대기",
  };
  const OS = { sel: { spinner: null, loom: null }, level: "all", follow: true };
  let followTimer = null;
  function buildOps(k) {
    const o = OPS[k];
    main.innerHTML = `<section class="screen ops"><div class="pagehead"><h1>${o.title}</h1></div>
    <div class="lede"><i></i>${o.lede}</div>
    <div class="opsbody"><div class="srclist" id="sl"></div>
    <div class="logpane"><div class="logbar"><span class="ttl" id="lt2"></span><span class="grow"></span>
      <div class="seg" role="group" aria-label="로그 수준"><button type="button" data-l="all">전체</button><button type="button" data-l="INFO">INFO</button><button type="button" data-l="WARNING">WARNING</button></div>
      <button class="toggle" type="button" id="fol" aria-pressed="true"><i></i>새 로그 따라가기</button></div>
      <div class="logout" id="lo" tabindex="0" aria-label="로그"></div></div></div></section>`;
    const sl = $("#sl");
    sl.innerHTML =
      `<button class="srcitem" data-id="" ${!OS.sel[k] ? 'aria-current="true"' : ""}><span class="a"><b style="font-weight:600">전체 source</b></span><span class="b">${o.list.length}개</span></button>` +
      o.list
        .map(
          (s) =>
            `<button class="srcitem" data-id="${esc(s.id)}" ${OS.sel[k] === s.id ? 'aria-current="true"' : ""}><span class="a"><span class="mono">${shortId(s.id)}</span></span><span class="b">${s.at}</span><span class="c"><span>${s.kind}</span><span class="st ${s.st}"><i></i>${ST[s.st]}</span></span></button>`,
        )
        .join("");
    $$(".srcitem", sl).forEach(
      (b) =>
        (b.onclick = () => {
          OS.sel[k] = b.dataset.id || null;
          buildOps(k);
          refreshOps(k);
        }),
    );
    $$(".logbar .seg button").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.l === OS.level));
      b.onclick = () => {
        OS.level = b.dataset.l;
        buildOps(k);
        refreshOps(k);
      };
    });
    $("#fol").setAttribute("aria-pressed", OS.follow);
    $("#fol").onclick = () => {
      OS.follow = !OS.follow;
      $("#fol").setAttribute("aria-pressed", OS.follow);
      if (OS.follow) {
        const lo = $("#lo");
        lo.scrollTop = lo.scrollHeight;
      }
    };
    const lo = $("#lo");
    lo.addEventListener("scroll", () => {
      if (OS.follow && lo.scrollHeight - lo.scrollTop - lo.clientHeight > 60) {
        OS.follow = false;
        $("#fol").setAttribute("aria-pressed", false);
      }
    });
    paintLog(k);
    clearInterval(followTimer);
    followTimer = setInterval(() => refreshOps(k), 5000);
  }
  let refreshGeneration = 0;
  async function refreshOps(k) {
    const generation = ++refreshGeneration;
    try {
      const params = new URLSearchParams({ component: k });
      if (OS.level !== "all") params.set("level", OS.level);
      if (OS.sel[k]) params.set("source_id", OS.sel[k]);
      const [logs, list, newStatus] = await Promise.all([
        api("/logs?" + params),
        api("/sources?stage=" + k),
        api("/status"),
      ]);
      if (
        S.screen !== k ||
        abort.signal.aborted ||
        generation !== refreshGeneration
      )
        return;
      const top = $("#lo")?.scrollTop || 0;
      Object.assign(status, newStatus);
      OPS[k] = makeOps(k, list, logs);
      renderNav();
      buildOps(k);
      if (!OS.follow) $("#lo").scrollTop = top;
    } catch {
      if (!abort.signal.aborted)
        showToast("서버 연결을 확인하세요. python -m weaver.app web");
    }
  }

  function paintLog(k) {
    const o = OPS[k],
      sel = OS.sel[k];
    const lines = o.logs.filter(
      (l) =>
        (OS.level === "all" || l.lv === OS.level) && (!sel || l.src === sel),
    );
    $("#lt2").textContent = sel
      ? `${shortId(sel)} 로그 ${lines.length}줄`
      : `전체 로그 ${lines.length}줄`;
    const lo = $("#lo");
    lo.innerHTML = lines.length
      ? lines
          .map(
            (l) =>
              `<span class="ll"><span class="tm">${l.ts}</span> <span class="lv ${l.lv === "WARNING" ? "w" : "i"}">${l.lv}</span> <span class="lg">${l.lg}:</span> ${esc(l.msg)}</span>`,
          )
          .join("")
      : '<span class="mut" style="font-family:inherit">조건에 맞는 로그가 없습니다.</span>';
    if (OS.follow) lo.scrollTop = lo.scrollHeight;
  }

  renderNav();
  const hash = location.pathname.split("/")[1];
  S.screen = ["spinner", "loom"].includes(hash) ? hash : "fabric";
  if (S.screen === "fabric") {
    buildFabric();
  } else buildOps(S.screen);
  renderNav();
  /* test hook */

  return () => {
    abort.abort();
    ro?.disconnect();
    stopPulse();
    clearInterval(followTimer);
    cancelAnimationFrame(raf);
    host.innerHTML = "";
  };
}
