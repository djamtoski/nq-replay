// Journal dialog: trade log with notes, analytics and a P&L calendar.
// All DOM is built with createElement/textContent, so trade notes and tags can never inject markup.
import { errorMessage } from "../errors";
import type { JournalApi } from "./api";
import {
  bucketLabel,
  calendarGrid,
  duration,
  equityPath,
  intensity,
  money,
  MONTHS,
  num,
  pct,
  pnlClass,
  shiftMonth,
  signedMoney,
  WEEKDAYS,
} from "./format";
import type {
  Analytics,
  Bucket,
  CalendarMonth,
  Filters,
  JTrade,
  Notes,
  Summary,
  Tags,
} from "./types";

export interface JournalDeps {
  api: JournalApi;
  /** Formats epoch seconds in the user's chosen time zone. */
  fmtTime: (sec: number) => string;
  /** Jump the chart to a trade. Resolves to an error message, or null on success. */
  onReplay: (tradeId: number) => Promise<string | null>;
  saveDelayMs?: number;
}

type Tab = "trades" | "analytics" | "calendar";
type Child = Node | string | null | false | undefined;

function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Record<string, unknown> = {},
  ...kids: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = String(v);
    else if (k.startsWith("on") && typeof v === "function")
      el.addEventListener(k.slice(2), v as EventListener);
    else if (k === "value") (el as unknown as HTMLInputElement).value = String(v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of kids) if (c) el.append(c);
  return el;
}

const svgNS = "http://www.w3.org/2000/svg";
function svg(tag: string, attrs: Record<string, string | number>): SVGElement {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

const BREAKDOWNS: [string, string][] = [
  ["setup", "By setup"],
  ["mistake", "By mistake"],
  ["emotion", "By emotion"],
  ["hour", "By entry hour (NY)"],
  ["weekday", "By weekday"],
  ["side", "By side"],
  ["reason", "By exit"],
];

export function createJournal(dlg: HTMLDialogElement, deps: JournalDeps) {
  const { api } = deps;
  const delay = deps.saveDelayMs ?? 500;

  let tab: Tab = "trades";
  let filters: Filters = {};
  let trades: JTrade[] = [];
  let selected: number | null = null;
  let analytics: Analytics | null = null;
  let tags: Tags = { setups: [], emotions: [], mistakes: [] };
  const now = new Date();
  const cal = { year: now.getFullYear(), month: now.getMonth() + 1 };
  let calData: CalendarMonth | null = null;
  let positioned = false;
  let seq = 0; // drops answers that arrive after the user already moved on

  // ---- save queue: one PATCH at a time, edits to the same trade are merged
  let pending: { id: number; notes: Notes } | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let chain: Promise<void> = Promise.resolve();
  let statusEl: HTMLElement | null = null;

  const setStatus = (text: string, bad = false) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = `jr-status${bad ? " bad" : ""}`;
  };

  function flush(): Promise<void> {
    clearTimeout(timer);
    timer = undefined;
    const job = pending;
    pending = null;
    if (!job) return chain;
    setStatus("Saving…");
    chain = chain.then(async () => {
      try {
        const saved = await api.saveNotes(job.id, job.notes);
        const t = trades.find((x) => x.id === job.id);
        if (t) Object.assign(t, saved);
        if (!pending) setStatus("Saved");
        renderList();
        void loadTags();
      } catch (e) {
        setStatus(`Not saved: ${errorMessage(e)}`, true);
      }
    });
    return chain;
  }

  function queueSave(id: number, patch: Notes, immediate = false): void {
    pending = { id, notes: { ...(pending?.notes ?? {}), ...patch } };
    setStatus("Editing…");
    clearTimeout(timer);
    if (immediate) void flush();
    else timer = setTimeout(() => void flush(), delay);
  }

  // ---- skeleton
  const err = h("div", { class: "jr-err", role: "alert" });
  const body = h("div", { class: "jr-body" });
  const tabs = h("div", { class: "seg", role: "tablist", "aria-label": "Journal sections" });
  const from = h("input", { type: "date", "aria-label": "From day" });
  const to = h("input", { type: "date", "aria-label": "To day" });
  const setupSel = h("select", { "aria-label": "Setup filter" });
  const sideSel = h("select", { "aria-label": "Side filter" });
  const datalists = h("div", { hidden: true });

  for (const [v, l] of [
    ["", "Both sides"],
    ["long", "Long"],
    ["short", "Short"],
  ])
    sideSel.append(h("option", { value: v }, l));

  const TABS: [Tab, string][] = [
    ["trades", "Trades"],
    ["analytics", "Analytics"],
    ["calendar", "Calendar"],
  ];
  for (const [id, label] of TABS)
    tabs.append(
      h(
        "button",
        { type: "button", role: "tab", "data-tab": id, onclick: () => void go(id) },
        label,
      ),
    );

  const clear = h(
    "button",
    {
      type: "button",
      onclick: () => {
        filters = {};
        syncFilterControls();
        void reload();
      },
    },
    "Clear",
  );
  const closeBtn = h(
    "button",
    { type: "button", onclick: () => close(), "aria-label": "Close journal" },
    "✕",
  );

  const applyFilters = () => {
    filters = {
      start: from.value || undefined,
      end: to.value || undefined,
      setup: setupSel.value || undefined,
      side: sideSel.value || undefined,
    };
    void reload();
  };
  for (const el of [from, to, setupSel, sideSel]) el.addEventListener("change", applyFilters);

  dlg.classList.add("jr");
  dlg.replaceChildren(
    h(
      "div",
      { class: "jr-head" },
      h("h2", {}, "Journal"),
      tabs,
      h("span", { class: "spacer" }),
      h("label", {}, "From ", from),
      h("label", {}, "To ", to),
      setupSel,
      sideSel,
      clear,
      closeBtn,
    ),
    err,
    body,
    datalists,
  );
  // closing with Esc or the backdrop must not lose an edit that is still waiting for its timer
  dlg.addEventListener("close", () => void flush());

  function syncFilterControls(): void {
    from.value = filters.start ?? "";
    to.value = filters.end ?? "";
    sideSel.value = filters.side ?? "";
    setupSel.replaceChildren(
      h("option", { value: "" }, "All setups"),
      ...tags.setups.map((s) => h("option", { value: s }, s)),
    );
    setupSel.value = filters.setup && tags.setups.includes(filters.setup) ? filters.setup : "";
    if (filters.setup && !setupSel.value) filters.setup = undefined;
  }

  function renderTagLists(): void {
    const dl = (id: string, items: string[]) =>
      h("datalist", { id }, ...items.map((v) => h("option", { value: v })));
    datalists.replaceChildren(
      dl("jr-setups", tags.setups),
      dl("jr-emotions", tags.emotions),
      dl("jr-mistakes", tags.mistakes),
    );
  }

  async function loadTags(): Promise<void> {
    try {
      tags = await api.tags();
    } catch {
      return; // autocomplete is a convenience
    }
    renderTagLists();
    syncFilterControls();
  }

  const showError = (e: unknown) => {
    err.textContent = e ? errorMessage(e) : "";
  };

  // ---- data
  async function reload(): Promise<void> {
    const my = ++seq;
    showError(null);
    try {
      if (tab === "trades") {
        const rows = await api.trades(filters);
        if (my !== seq) return;
        trades = rows;
        if (!trades.some((t) => t.id === selected)) selected = trades[0]?.id ?? null;
      } else if (tab === "analytics") {
        const a = await api.analytics(filters);
        if (my !== seq) return;
        analytics = a;
      } else {
        const c = await api.calendar(cal.year, cal.month);
        if (my !== seq) return;
        calData = c;
      }
    } catch (e) {
      if (my === seq) showError(e);
      return;
    }
    render();
  }

  async function go(t: Tab): Promise<void> {
    await flush();
    tab = t;
    await reload();
  }

  // ---- rendering
  function render(): void {
    tabs.querySelectorAll("button").forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", String(on));
    });
    statusEl = null;
    if (tab === "trades") renderTrades();
    else if (tab === "analytics") renderAnalytics();
    else renderCalendar();
  }

  // ---------------- trades
  let listBody: HTMLTableSectionElement | null = null;
  let detailBox: HTMLElement | null = null;

  function renderTrades(): void {
    listBody = h("tbody");
    detailBox = h("div", { class: "jr-detail" });
    const head = ["Closed", "Side", "Qty", "Entry", "Exit", "P&L", "R", "Setup", "★"];
    body.replaceChildren(
      h(
        "div",
        { class: "jr-trades" },
        h(
          "div",
          { class: "jr-list" },
          h("table", {}, h("thead", {}, h("tr", {}, ...head.map((x) => h("th", {}, x)))), listBody),
        ),
        detailBox,
      ),
    );
    renderList();
    renderDetail();
  }

  function renderList(): void {
    if (!listBody) return;
    if (!trades.length) {
      listBody.replaceChildren(
        h(
          "tr",
          {},
          h(
            "td",
            { class: "empty", colspan: 9 },
            "No trades yet. Closed trades from your replays appear here automatically.",
          ),
        ),
      );
      return;
    }
    listBody.replaceChildren(
      ...trades.map((t) =>
        h(
          "tr",
          {
            class: t.id === selected ? "sel" : "",
            tabindex: 0,
            "data-id": t.id,
            onclick: () => void select(t.id),
            onkeydown: (e: Event) => {
              const k = (e as KeyboardEvent).key;
              if (k === "Enter" || k === " ") {
                e.preventDefault();
                void select(t.id);
              }
            },
          },
          h("td", {}, deps.fmtTime(t.closed)),
          h("td", { class: t.side === "long" ? "pos" : "neg" }, t.side),
          h("td", {}, String(t.qty)),
          h("td", {}, num(t.entry)),
          h("td", {}, num(t.exit)),
          h("td", { class: pnlClass(t.pnl) }, signedMoney(t.pnl, 2)),
          h("td", {}, num(t.r)),
          h("td", {}, t.setup ?? ""),
          h("td", {}, t.rating ? String(t.rating) : ""),
        ),
      ),
    );
  }

  async function select(id: number): Promise<void> {
    if (id === selected) return;
    await flush();
    selected = id;
    renderList();
    renderDetail();
  }

  function fact(label: string, value: string, cls = ""): HTMLElement {
    return h("div", {}, h("small", {}, label), h("b", { class: cls }, value));
  }

  function renderDetail(): void {
    if (!detailBox) return;
    const t = trades.find((x) => x.id === selected);
    if (!t) {
      statusEl = null;
      detailBox.replaceChildren(h("p", { class: "muted" }, "Select a trade to add notes."));
      return;
    }
    const patch = (n: Notes, immediate = false) => queueSave(t.id, n, immediate);

    const text = (
      label: string,
      key: "setup" | "emotion",
      list: string,
      placeholder: string,
    ): HTMLElement => {
      const input = h("input", {
        type: "text",
        maxlength: 40,
        list,
        placeholder,
        value: t[key] ?? "",
        oninput: () => {
          t[key] = input.value.trim() || null;
          patch({ [key]: input.value });
        },
        onblur: () => void flush(),
      });
      return h("label", {}, label, input);
    };

    const stars = h("div", { class: "jr-stars", role: "group", "aria-label": "Rating" });
    const paintStars = () => {
      stars.replaceChildren(
        ...[1, 2, 3, 4, 5].map((n) =>
          h(
            "button",
            {
              type: "button",
              class: (t.rating ?? 0) >= n ? "on" : "",
              "aria-label": `${n} star${n > 1 ? "s" : ""}`,
              "aria-pressed": String(t.rating === n),
              onclick: () => {
                t.rating = t.rating === n ? null : n;
                paintStars();
                patch({ rating: t.rating }, true);
              },
            },
            "★",
          ),
        ),
      );
    };
    paintStars();

    const chips = h("div", { class: "jr-chips" });
    const paintChips = () => {
      chips.replaceChildren(
        ...t.mistakes.map((m) =>
          h(
            "span",
            { class: "chip" },
            m,
            h(
              "button",
              {
                type: "button",
                "aria-label": `Remove ${m}`,
                onclick: () => {
                  t.mistakes = t.mistakes.filter((x) => x !== m);
                  paintChips();
                  patch({ mistakes: t.mistakes }, true);
                },
              },
              "×",
            ),
          ),
        ),
      );
    };
    paintChips();
    const addMistake = () => {
      const v = mistakeIn.value.trim();
      mistakeIn.value = "";
      if (!v || t.mistakes.includes(v) || t.mistakes.length >= 10) return;
      t.mistakes = [...t.mistakes, v.slice(0, 40)];
      paintChips();
      patch({ mistakes: t.mistakes }, true);
    };
    const mistakeIn = h("input", {
      type: "text",
      maxlength: 40,
      list: "jr-mistakes",
      placeholder: "Add a mistake, press Enter",
      onkeydown: (e: Event) => {
        const k = e as KeyboardEvent;
        if (k.key === "Enter" || k.key === ",") {
          k.preventDefault();
          addMistake();
        }
      },
      onchange: addMistake,
    });

    const note = h("textarea", {
      rows: 6,
      maxlength: 5000,
      placeholder: "What was the plan? What did you see? What would you change?",
      value: t.note,
      oninput: () => {
        t.note = note.value;
        patch({ note: note.value });
      },
      onblur: () => void flush(),
    });

    statusEl = h("span", { class: "jr-status", role: "status" });
    const replayBtn = h(
      "button",
      {
        type: "button",
        class: "primary",
        onclick: async () => {
          replayBtn.disabled = true;
          await flush();
          const e = await deps.onReplay(t.id);
          replayBtn.disabled = false;
          if (e) showError(e);
          else close();
        },
      },
      "Replay this trade",
    );
    const delBtn = h(
      "button",
      {
        type: "button",
        class: "danger",
        onclick: async () => {
          if (!window.confirm("Delete this trade from the journal? This cannot be undone.")) return;
          pending = null;
          clearTimeout(timer);
          try {
            await api.remove(t.id);
          } catch (e) {
            showError(e);
            return;
          }
          trades = trades.filter((x) => x.id !== t.id);
          selected = trades[0]?.id ?? null;
          renderList();
          renderDetail();
          void loadTags();
        },
      },
      "Delete",
    );

    detailBox.replaceChildren(
      h(
        "h3",
        {},
        `${t.side === "long" ? "Long" : "Short"} ${t.qty} ${t.instrument}`,
        h("span", { class: `jr-pnl ${pnlClass(t.pnl)}` }, signedMoney(t.pnl, 2)),
      ),
      h(
        "div",
        { class: "jr-facts" },
        fact("Opened", deps.fmtTime(t.opened)),
        fact("Closed", deps.fmtTime(t.closed)),
        fact("Entry → exit", `${num(t.entry)} → ${num(t.exit)}`),
        fact("Held", duration(t.closed - t.opened)),
        fact("R", num(t.r), pnlClass(t.r)),
        fact("Exit", t.reason),
        fact("MAE", `${money(t.mae_usd)} (${num(t.mae)} pts)`, t.mae_usd ? "neg" : ""),
        fact("MFE", `${money(t.mfe_usd)} (${num(t.mfe)} pts)`, t.mfe_usd ? "pos" : ""),
      ),
      h(
        "div",
        { class: "jr-form" },
        text("Setup", "setup", "jr-setups", "e.g. Opening range"),
        text("Emotion", "emotion", "jr-emotions", "e.g. calm, FOMO"),
      ),
      h("div", { class: "jr-field" }, h("small", {}, "Rating"), stars),
      h("div", { class: "jr-field" }, h("small", {}, "Mistakes"), chips, mistakeIn),
      h("label", { class: "jr-field" }, h("small", {}, "Notes"), note),
      h(
        "div",
        { class: "jr-actions" },
        replayBtn,
        delBtn,
        h("span", { class: "spacer" }),
        statusEl,
      ),
    );
  }

  // ---------------- analytics
  function statCard(label: string, value: string, cls = ""): HTMLElement {
    return h("div", { class: "card2" }, h("small", {}, label), h("b", { class: cls }, value));
  }

  function summaryCards(s: Summary): HTMLElement[] {
    return [
      statCard("Net P&L", signedMoney(s.net_pnl, 2), pnlClass(s.net_pnl)),
      statCard("Trades", String(s.trades)),
      statCard("Win rate", pct(s.win_rate, 1)),
      statCard("Profit factor", s.profit_factor == null ? "–" : num(s.profit_factor)),
      statCard("Expectancy", signedMoney(s.expectancy, 2), pnlClass(s.expectancy)),
      statCard("Avg R", num(s.avg_r), pnlClass(s.avg_r)),
      statCard("Avg win", money(s.avg_win, 2), "pos"),
      statCard("Avg loss", money(s.avg_loss, 2), "neg"),
      statCard("Best / worst", `${money(s.best)} / ${money(s.worst)}`),
      statCard("Max drawdown", money(s.max_drawdown, 2), "neg"),
      statCard("Avg hold", duration(s.avg_hold_s ?? NaN)),
      statCard("Avg MAE / MFE", `${money(s.avg_mae_usd)} / ${money(s.avg_mfe_usd)}`),
    ];
  }

  function equityChart(a: Analytics): SVGElement {
    const W = 720;
    const H = 180;
    const c = equityPath(a.equity, W, H);
    const el = svg("svg", {
      viewBox: `0 0 ${W} ${H}`,
      class: "jr-equity",
      role: "img",
      "aria-label": "Cumulative P&L",
    });
    el.append(
      svg("line", { x1: 0, x2: W, y1: c.zeroY, y2: c.zeroY, class: "zero" }),
      svg("path", { d: c.d, class: "curve", fill: "none" }),
    );
    const title = svg("title", {});
    title.textContent = `Cumulative P&L: low ${money(c.min)}, high ${money(c.max)}`;
    el.append(title);
    return el;
  }

  function breakdownTable(kind: string, title: string, rows: Bucket[]): HTMLElement | null {
    if (!rows.length) return null;
    const maxAbs = Math.max(...rows.map((r) => Math.abs(r.pnl))) || 1;
    return h(
      "section",
      { class: "card2 wide" },
      h("h4", {}, title),
      h(
        "table",
        {},
        h(
          "thead",
          {},
          h("tr", {}, ...["", "Trades", "Win %", "Avg R", "P&L", ""].map((x) => h("th", {}, x))),
        ),
        h(
          "tbody",
          {},
          ...rows.map((r) =>
            h(
              "tr",
              {},
              h("td", {}, bucketLabel(kind, r.key)),
              h("td", {}, String(r.trades)),
              h("td", {}, pct(r.win_rate)),
              h("td", {}, num(r.avg_r)),
              h("td", { class: pnlClass(r.pnl) }, signedMoney(r.pnl, 0)),
              h(
                "td",
                { class: "barcell" },
                h("span", {
                  class: `bar ${r.pnl >= 0 ? "pos" : "neg"}`,
                  style: `width:${Math.round((Math.abs(r.pnl) / maxAbs) * 100)}%`,
                }),
              ),
            ),
          ),
        ),
      ),
    );
  }

  function renderAnalytics(): void {
    const a = analytics;
    if (!a || !a.summary.trades) {
      body.replaceChildren(
        h(
          "p",
          { class: "empty" },
          "No trades match. Take some trades in a replay, or clear the filters.",
        ),
      );
      return;
    }
    body.replaceChildren(
      h(
        "div",
        { class: "jr-analytics" },
        h("div", { class: "jr-cards" }, ...summaryCards(a.summary)),
        h("section", { class: "card2 wide" }, h("h4", {}, "Cumulative P&L"), equityChart(a)),
        h(
          "div",
          { class: "jr-breakdowns" },
          ...BREAKDOWNS.map(([k, t]) => breakdownTable(k, t, a.breakdowns[k] ?? [])),
        ),
      ),
    );
  }

  // ---------------- calendar
  function renderCalendar(): void {
    const c = calData;
    const weeks = calendarGrid(cal.year, cal.month, c?.days ?? []);
    const maxAbs = Math.max(0, ...(c?.days ?? []).map((d) => Math.abs(d.pnl)));
    const nav = (delta: number) => () => {
      [cal.year, cal.month] = shiftMonth(cal.year, cal.month, delta);
      void reload();
    };
    body.replaceChildren(
      h(
        "div",
        { class: "jr-cal" },
        h(
          "div",
          { class: "jr-calhead" },
          h("button", { type: "button", "aria-label": "Previous month", onclick: nav(-1) }, "‹"),
          h("h3", {}, `${MONTHS[cal.month - 1]} ${cal.year}`),
          h("button", { type: "button", "aria-label": "Next month", onclick: nav(1) }, "›"),
          h("span", { class: "spacer" }),
          h("span", { class: pnlClass(c?.net_pnl) }, `${signedMoney(c?.net_pnl ?? 0, 2)}`),
          h(
            "span",
            { class: "muted" },
            `${c?.trades ?? 0} trades · ${c?.green_days ?? 0} green · ${c?.red_days ?? 0} red days`,
          ),
        ),
        h(
          "table",
          { class: "calgrid" },
          h("thead", {}, h("tr", {}, ...WEEKDAYS.map((d) => h("th", {}, d)))),
          h(
            "tbody",
            {},
            ...weeks.map((w) =>
              h(
                "tr",
                {},
                ...w.map((cell) => {
                  if (!cell) return h("td", { class: "blank" });
                  const has = cell.trades > 0;
                  const a = intensity(cell.pnl, maxAbs);
                  return h(
                    "td",
                    {
                      class: `day${has ? " has" : ""}`,
                      "data-day": cell.day,
                      tabindex: has ? 0 : null,
                      role: has ? "button" : null,
                      style: has
                        ? `background:color-mix(in srgb, var(${cell.pnl >= 0 ? "--buy" : "--sell"}) ${Math.round(a * 100)}%, transparent)`
                        : null,
                      onclick: has ? () => openDay(cell.day) : null,
                      onkeydown: has
                        ? (e: Event) => {
                            if ((e as KeyboardEvent).key === "Enter") openDay(cell.day);
                          }
                        : null,
                    },
                    h("span", { class: "n" }, String(cell.n)),
                    has && h("b", {}, signedMoney(cell.pnl, 0)),
                    has && h("small", {}, `${cell.trades} trade${cell.trades > 1 ? "s" : ""}`),
                  );
                }),
              ),
            ),
          ),
        ),
      ),
    );
  }

  function openDay(day: string): void {
    filters = { ...filters, start: day, end: day };
    syncFilterControls();
    void go("trades");
  }

  // ---- public
  async function open(): Promise<void> {
    if (!dlg.open) dlg.showModal();
    showError(null);
    await loadTags();
    syncFilterControls();
    if (!positioned) {
      // the calendar starts on the month of the newest trade, not on today's (possibly empty) month
      positioned = true;
      try {
        const [latest] = await api.trades({}, 1);
        if (latest) {
          cal.year = Number(latest.day.slice(0, 4));
          cal.month = Number(latest.day.slice(5, 7));
        }
      } catch {
        /* the reload below reports connection problems */
      }
    }
    await reload();
  }

  function close(): void {
    void flush();
    if (dlg.open) dlg.close();
  }

  return {
    open,
    close,
    flush,
    /** Test hooks and callers that change data elsewhere. */
    reload,
    go,
  };
}

export type Journal = ReturnType<typeof createJournal>;
