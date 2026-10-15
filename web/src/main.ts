import { CandlestickSeries, ColorType, createChart, CrosshairMode, LineStyle, type CandlestickData, type IPriceLine, type UTCTimestamp } from "lightweight-charts";
import { apply, bracket, fmt, initial, nextCandleEnd, prevCandleEnd, type Bar, type ServerMsg, type State } from "./store";
import { CPS, cpsLabel, effectiveCps, feasible, replayRate } from "./speed";
import { DrawingManager } from "./drawings/manager";

const API = (import.meta.env.VITE_API as string | undefined) ?? "http://127.0.0.1:8000";
const TFS: [number, string][] = [[1, "1s"], [15, "15s"], [60, "1m"], [300, "5m"], [900, "15m"], [3600, "1h"], [14400, "4h"]];
const PC_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

// ---- tiny safe localStorage wrapper (can throw in private windows)
const store = {
  get<T>(k: string, d: T): T { try { const v = localStorage.getItem(k); return v ? (JSON.parse(v) as T) : d; } catch { return d; } },
  set(k: string, v: unknown): void { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
};

let st: State = initial();
let ws: WebSocket | null = null;
let tf: number = store.get("tf", 60);
if (!TFS.some(([s]) => s === tf)) tf = 60;
let pointValue = 20;
let orderType: "market" | "limit" | "stop" = "market";
let hover: CandlestickData | null = null;
let replayStart = 0;     // replay time (epoch s) of the first reset: the "now" the session began at
let times: number[] = [];   // bar times currently in the series, in order (drawings map time <-> bar index with this)

// ================================================================ chart
const css = (v: string) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
// Display zone: default is this PC's zone. Market data instants are absolute; only the label changes.
const TZ_CHOICES = ["pc", "America/New_York", "UTC"];
let tzMode: string = store.get("tz", "pc");
if (!TZ_CHOICES.includes(tzMode)) tzMode = "pc";     // a bad stored value would make Intl throw RangeError
const zone = () => (tzMode === "pc" ? PC_ZONE : tzMode);
const fmtCache = new Map<string, Intl.DateTimeFormat>();
const tzTime = (t: number, o: Intl.DateTimeFormatOptions) => {
  const key = zone() + JSON.stringify(o);
  let f = fmtCache.get(key);
  if (!f) { f = new Intl.DateTimeFormat("en-US", { timeZone: zone(), hour12: false, ...o }); fmtCache.set(key, f); }
  return f.format(t * 1000);
};
const nameFmt = new Map<string, Intl.DateTimeFormat>();     // one formatter per zone (construction is slow)
const tzName = (t: number) => {
  let f = nameFmt.get(zone());
  if (!f) { f = new Intl.DateTimeFormat("en-US", { timeZone: zone(), timeZoneName: "short" }); nameFmt.set(zone(), f); }
  return f.formatToParts(t * 1000).find((p) => p.type === "timeZoneName")?.value ?? "";
};
const LONG: Intl.DateTimeFormatOptions = { weekday: "short", year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" };
const longTime = (t: number) => `${tzTime(t, LONG)} ${tzName(t)}`;
// lightweight-charts TickMarkType: 0 Year, 1 Month, 2 DayOfMonth, 3 Time, 4 TimeWithSeconds
const tickFmt = (t: number, type: number) =>
  type === 0 ? tzTime(t, { year: "numeric" })
  : type === 1 ? tzTime(t, { month: "short", year: "numeric" })
  : type === 2 ? tzTime(t, { month: "short", day: "numeric" })
  : type === 3 ? tzTime(t, { hour: "2-digit", minute: "2-digit" })
  : tzTime(t, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

const chart = createChart($("chart"), {
  autoSize: true,
  layout: { background: { type: ColorType.Solid, color: css("--bg") }, textColor: css("--muted"), fontSize: 12 },
  grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
  crosshair: { mode: CrosshairMode.Normal },
  timeScale: { timeVisible: true, secondsVisible: true, borderColor: css("--line"), rightOffset: 10,
    tickMarkFormatter: (t: number, type: number) => tickFmt(t, type) },
  rightPriceScale: { borderColor: css("--line"), scaleMargins: { top: 0.08, bottom: 0.12 } },
  localization: { timeFormatter: (t: number) => longTime(t) },
});
const series = chart.addSeries(CandlestickSeries, {
  upColor: css("--buy"), downColor: css("--sell"), borderVisible: false,
  wickUpColor: css("--buy"), wickDownColor: css("--sell"),
});
const toC = (b: Bar): CandlestickData => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c });

function redrawChart(): void {
  const data = (st.bars[tf] ?? []).map(toC);
  const f = st.forming[tf];
  if (f) data.push(toC(f));
  series.setData(data);
  times = data.map((d) => d.time as number);
  const n = data.length;
  chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 130), to: n + 10 });
}

/** Update the series and keep `times` mirroring it exactly (same-time update replaces, later time appends). */
function pushBar(b: Bar): void {
  series.update(toC(b));
  if (times[times.length - 1] !== b.t) times.push(b.t);
}

const drawings = new DrawingManager({
  chart, series, container: $("chart"), toolbar: $("drawbar"), propbar: $("propbar"), textInput: $<HTMLInputElement>("textInput"),
  getTimes: () => times, getInterval: () => tf, getPointValue: () => pointValue, fmtPrice: (p) => fmt(p),
  notify: (m) => { st.lastError = m; scheduleRender(); },
});

chart.subscribeCrosshairMove((p) => {
  hover = (p.time ? (p.seriesData.get(series) as CandlestickData | undefined) : undefined) ?? null;
  renderLegend();
});

function renderLegend(): void {
  const f = st.forming[tf];
  const d = hover ?? (f ? toC(f) : (st.bars[tf]?.at(-1) ? toC(st.bars[tf].at(-1)!) : null));
  if (!d) { $("legend").innerHTML = ""; return; }
  const up = d.close >= d.open;
  const cls = up ? "pos" : "neg";
  const chg = d.close - d.open;
  $("legend").innerHTML =
    `<b>NQ</b><span class="muted">${TFS.find(([s]) => s === tf)?.[1]}</span>` +
    `<span><span class="k">O</span><span class="${cls}">${fmt(d.open)}</span></span>` +
    `<span><span class="k">H</span><span class="${cls}">${fmt(d.high)}</span></span>` +
    `<span><span class="k">L</span><span class="${cls}">${fmt(d.low)}</span></span>` +
    `<span><span class="k">C</span><span class="${cls}">${fmt(d.close)}</span></span>` +
    `<span class="${cls}">${chg >= 0 ? "+" : ""}${fmt(chg)}</span>`;
}

// ================================================================ price-line overlays
const lines = new Map<string, IPriceLine>();
const lineSig = new Map<string, string>();
function setLine(key: string, price: number | null, color: string, title: string, style: LineStyle): void {
  const sig = `${price}|${title}|${color}`;
  if (lineSig.get(key) === sig) return;          // unchanged: skip (matters at fast playback)
  const cur = lines.get(key);
  if (cur) { series.removePriceLine(cur); lines.delete(key); }
  lineSig.set(key, sig);
  if (price !== null && Number.isFinite(price)) {
    lines.set(key, series.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  }
}
function clearLines(): void {
  for (const l of lines.values()) series.removePriceLine(l);
  lines.clear(); lineSig.clear();
}
function drawOverlays(): void {
  const want = new Map<string, [number | null, string, string, LineStyle]>();
  const a = st.sessions?.asia;
  want.set("asia-hi", [a?.hi ?? null, css("--asia"), "Asia H", LineStyle.Dotted]);
  want.set("asia-lo", [a?.lo ?? null, css("--asia"), "Asia L", LineStyle.Dotted]);
  for (const p of st.positions) {
    want.set(`p${p.id}-e`, [p.entry, p.side === "long" ? css("--buy") : css("--sell"), `${p.side === "long" ? "LONG" : "SHORT"} ${p.qty}`, LineStyle.Solid]);
    want.set(`p${p.id}-sl`, [p.sl, css("--sell"), "SL", LineStyle.Dashed]);
    want.set(`p${p.id}-tp`, [p.tp, css("--buy"), "TP", LineStyle.Dashed]);
  }
  for (const o of st.orders) want.set(`o${o.id}`, [o.price, css("--accent"), `${o.otype} ${o.qty}`, LineStyle.Dashed]);
  for (const k of [...lineSig.keys()]) if (!want.has(k)) setLine(k, null, "", "", LineStyle.Solid);
  for (const [k, [price, color, title, style]] of want) setLine(k, price, color, title, style);
}

// ================================================================ panels
const sideCls = (n: number) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
const lastPrice = () => st.forming[tf]?.c ?? st.bars[tf]?.at(-1)?.c ?? st.forming["1"]?.c ?? null;

function renderPanels(): void {
  $("clock").textContent = st.now ? longTime(st.now) : "";
  const el = Math.max(0, st.now - replayStart);
  $("pos").textContent = st.total ? `+${Math.floor(el / 3600)}h ${String(Math.floor((el % 3600) / 60)).padStart(2, "0")}m replayed${st.ended ? " · end of data" : ""}` : "";
  const play = $<HTMLButtonElement>("play");
  play.textContent = st.playback === "playing" ? "⏸" : "▶";
  play.disabled = st.ended;
  $("err").textContent = st.lastError ?? "";
  const lp = lastPrice();
  $("buyPx").textContent = lp === null ? "" : fmt(lp);
  $("sellPx").textContent = lp === null ? "" : fmt(lp);

  const a = st.account;
  if (a) {
    const s = a.stats;
    const rows: [string, string, string?][] = [
      ["Balance", `$${fmt(a.balance)}`], ["Equity", `$${fmt(a.equity)}`],
      ["Unrealized", `$${fmt(a.unrealized)}`, sideCls(a.unrealized)], ["Margin used", `$${fmt(a.margin, 0)}`],
      ["Trades", `${s.trades}`], ["Win rate", s.win_rate == null ? "–" : `${fmt(s.win_rate * 100, 1)}%`],
      ["Net P&L", s.net_pnl == null ? "–" : `$${fmt(s.net_pnl)}`, sideCls(s.net_pnl ?? 0)],
      ["Profit factor", s.profit_factor == null ? "–" : fmt(s.profit_factor)],
      ["Avg R", fmt(s.avg_r)], ["Max drawdown", s.max_drawdown_usd == null ? "–" : `$${fmt(s.max_drawdown_usd)}`],
    ];
    $("stats").innerHTML = rows.map(([k, v, c]) => `<dt>${k}</dt><dd class="${c ?? ""}">${v}</dd>`).join("");
  }

  $("nPos").textContent = String(st.positions.length);
  $("nOrd").textContent = String(st.orders.length);
  $("nTr").textContent = String(st.trades.length);
  const empty = (cols: number, msg: string) => `<tr><td class="empty" colspan="${cols}">${msg}</td></tr>`;
  const tbody = (id: string) => document.querySelector(`#${id} tbody`) as HTMLElement;

  tbody("tPositions").innerHTML = st.positions.length ? st.positions.map((p) => {
    const dir = p.side === "long" ? 1 : -1;
    const pnl = lp === null ? 0 : (lp - p.entry) * dir * p.qty * pointValue;
    return `<tr><td class="${p.side === "long" ? "pos" : "neg"}">${p.side}</td><td>${p.qty}</td><td>${fmt(p.entry)}</td>
      <td>${fmt(p.sl)}</td><td>${fmt(p.tp)}</td><td class="${sideCls(pnl)}">${fmt(pnl)}</td>
      <td><button data-close="${p.id}">Close</button></td></tr>`;
  }).join("") : empty(7, "No open positions");

  tbody("tOrders").innerHTML = st.orders.length ? st.orders.map((o) =>
    `<tr><td>${o.otype}</td><td>${o.side === "long" ? "buy" : "sell"}</td><td>${o.qty}</td><td>${fmt(o.price)}</td>
     <td>${o.status}</td><td><button data-cancel="${o.id}">Cancel</button></td></tr>`).join("") : empty(6, "No working orders");

  tbody("tTrades").innerHTML = st.trades.length ? st.trades.slice(-200).reverse().map((t) =>
    `<tr><td>${tzTime(t.closed, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
     <td>${t.side}</td><td>${t.qty}</td><td>${fmt(t.entry)}</td><td>${fmt(t.exit)}</td>
     <td class="${sideCls(t.pnl)}">${fmt(t.pnl)}</td><td>${fmt(t.r)}</td><td>${t.reason}</td></tr>`).join("") : empty(8, "No closed trades yet");
  renderLegend();
}

let renderTimer = 0;
function scheduleRender(): void {            // ~10 fps is plenty for DOM panels; the chart itself updates immediately
  if (renderTimer) return;
  renderTimer = window.setTimeout(() => { renderTimer = 0; drawOverlays(); renderPanels(); }, 100);
}

// ================================================================ websocket / session
const send = (o: object): void => { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(o)); };
function setConn(text: string, kind: "on" | "bad" | "" = ""): void {
  const c = $("conn"); c.textContent = text; c.className = `pill ${kind}`;
}

function onMessage(raw: string): void {
  let m: ServerMsg;
  try { m = JSON.parse(raw) as ServerMsg; } catch (e) { console.error("bad server message", e, raw.slice(0, 200)); return; }
  apply(st, m);
  if (m.type === "reset") {
    if (!replayStart) replayStart = m.now;
    redrawChart();
  } else if (m.type === "tick") {
    for (const b of m.closed[String(tf)] ?? []) pushBar(b);
    const f = st.forming[String(tf)];
    if (f) pushBar(f);
  }
  scheduleRender();
}

interface SessionCfg { source: string; instrument: string; startTime: string; balance: number; start?: string; end?: string }

async function newSession(cfg: SessionCfg): Promise<string | null> {
  ws?.close(); ws = null;
  clearLines(); st = initial(); replayStart = 0; series.setData([]); times = []; hover = null; renderLegend(); scheduleRender();
  pointValue = cfg.instrument === "MNQ" ? 2 : 20;
  drawings.load(`drawings:v1:${cfg.source}:${cfg.instrument}`);
  $("symbol").textContent = `${cfg.instrument} · CME`;
  setConn("creating session…");
  const body: Record<string, unknown> = { source: cfg.source, instrument: cfg.instrument, start_time: cfg.startTime,
    balance: cfg.balance, tfs: TFS.map(([s]) => s) };
  if (cfg.source === "parquet") { body.start = cfg.start; body.end = cfg.end; }
  let sid: string;
  try {
    const r = await fetch(`${API}/sessions`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(typeof j.detail === "string" ? j.detail : r.statusText);
    }
    sid = (await r.json()).id;
  } catch (e) {
    const msg = (e as Error).message;
    setConn(`server error`, "bad");
    return msg === "Failed to fetch" ? `Can't reach the API at ${API}. Is the server running?` : msg;
  }
  const sock = new WebSocket(`${API.replace(/^http/, "ws")}/ws/${sid}`);
  ws = sock;
  sock.onopen = () => { setConn("connected", "on"); syncSpeed(); };
  sock.onclose = () => { if (ws === sock) setConn("disconnected", "bad"); };
  sock.onmessage = (ev) => onMessage(ev.data as string);
  return null;
}

// ================================================================ UI wiring
// timeframe buttons
$("tfs").innerHTML = TFS.map(([s, l]) => `<button data-tf="${s}" class="${s === tf ? "active" : ""}">${l}</button>`).join("");
$("tfs").addEventListener("click", (e) => {
  const v = Number((e.target as HTMLElement).dataset.tf);
  if (!v || v === tf) return;
  tf = v; store.set("tf", v);
  document.querySelectorAll("#tfs button").forEach((b) => b.classList.toggle("active", Number((b as HTMLElement).dataset.tf) === v));
  hover = null; redrawChart(); syncSpeed(); scheduleRender();
});

// replay dock
let cpsPref: number = store.get("cps", 1);       // what the user chose; never overwritten by clamping
if (!CPS.includes(cpsPref)) cpsPref = 1;
/** Rebuild the speed menu for the current timeframe (options the server can't keep up with are disabled) and tell the server. */
function syncSpeed(): void {
  const sp = $<HTMLSelectElement>("speed");
  const cps = effectiveCps(cpsPref, tf);
  sp.innerHTML = CPS.map((v) => `<option value="${v}" ${feasible(v, tf) ? "" : "disabled"}>${cpsLabel(v)}${feasible(v, tf) ? "" : " (too fast)"}</option>`).join("");
  sp.value = String(cps);
  send({ cmd: "speed", value: replayRate(cps, tf) });
}
const togglePlay = () => send({ cmd: st.playback === "playing" ? "pause" : "play" });
const stepCandle = (dir: 1 | -1) => send({ cmd: "seek", ts: dir > 0 ? nextCandleEnd(st.now, tf) : prevCandleEnd(st.now, tf) });
$("play").onclick = togglePlay;
$("nextCandle").onclick = () => stepCandle(1);
$("prevCandle").onclick = () => stepCandle(-1);
$<HTMLSelectElement>("speed").onchange = (e) => { cpsPref = Number((e.target as HTMLSelectElement).value); store.set("cps", cpsPref); syncSpeed(); };

// time zone selector: PC zone by default, or exchange (New York) / UTC
const tzSel = $<HTMLSelectElement>("tz");
tzSel.innerHTML = `<option value="pc">PC time (${PC_ZONE})</option><option value="America/New_York">New York</option><option value="UTC">UTC</option>`;
tzSel.value = tzMode;
tzSel.onchange = () => {
  tzMode = tzSel.value; store.set("tz", tzMode); fmtCache.clear();
  chart.applyOptions({ localization: { timeFormatter: (t: number) => longTime(t) }, timeScale: { tickMarkFormatter: (t: number, type: number) => tickFmt(t, type) } });
  scheduleRender();
};

// header
const toggleFullscreen = () => { if (document.fullscreenElement) void document.exitFullscreen(); else void document.documentElement.requestFullscreen?.(); };
$("fullscreen").onclick = toggleFullscreen;
document.addEventListener("fullscreenchange", () => { $("fullscreen").textContent = document.fullscreenElement ? "✕ Exit full screen" : "⛶ Full screen"; });
$("togglePanels").onclick = () => $("app").classList.toggle("focus");

// drawer tabs
$("drawer").querySelector(".tabs")!.addEventListener("click", (e) => {
  const t = (e.target as HTMLElement).dataset.tab;
  if (!t) return;
  document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.classList.toggle("active", (b as HTMLElement).dataset.tab === t));
  ($("tPositions") as HTMLElement).hidden = t !== "positions";
  ($("tOrders") as HTMLElement).hidden = t !== "orders";
  ($("tTrades") as HTMLElement).hidden = t !== "trades";
});

// order ticket
$("otype").addEventListener("click", (e) => {
  const t = (e.target as HTMLElement).dataset.t as typeof orderType | undefined;
  if (!t) return;
  orderType = t;
  document.querySelectorAll("#otype button").forEach((b) => b.classList.toggle("active", (b as HTMLElement).dataset.t === t));
  const p = $<HTMLInputElement>("price");
  p.disabled = t === "market";
  p.placeholder = t === "market" ? "market" : "price";
});
const num = (id: string): number | null => { const v = $<HTMLInputElement>(id).value; return v === "" ? null : Number(v); };
function placeOrder(side: "long" | "short"): void {
  const price = orderType === "market" ? null : num("price");
  if (orderType !== "market" && price === null) { st.lastError = "Enter a price for limit/stop orders."; scheduleRender(); return; }
  const ref = price ?? lastPrice() ?? 0;
  const { sl, tp } = bracket(side, ref, num("sl"), num("tp"));
  st.lastError = null;
  send({ cmd: "order", side, qty: num("qty") ?? 1, otype: orderType, price, sl, tp, trail_ticks: num("trail") });
}
$("buy").onclick = () => placeOrder("long");
$("sell").onclick = () => placeOrder("short");
$("flatten").onclick = () => {
  st.positions.forEach((p) => send({ cmd: "close", position_id: p.id }));
  st.orders.forEach((o) => send({ cmd: "cancel", order_id: o.id }));
};
document.addEventListener("click", (e) => {
  const t = e.target as HTMLElement;
  if (t.dataset.close) send({ cmd: "close", position_id: Number(t.dataset.close) });
  if (t.dataset.cancel) send({ cmd: "cancel", order_id: Number(t.dataset.cancel) });
});

// keyboard (ignored while typing or while the dialog is open)
document.addEventListener("keydown", (e) => {
  const t = e.target as HTMLElement;
  if (t.matches("input,select,textarea") || ($("sessionDlg") as HTMLDialogElement).open || e.ctrlKey || e.metaKey) return;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.code === "ArrowRight") { e.preventDefault(); stepCandle(1); }
  else if (e.code === "ArrowLeft") { e.preventDefault(); stepCandle(-1); }
  else if (e.key === "f" || e.key === "F") toggleFullscreen();
});

// session dialog
const dlg = $<HTMLDialogElement>("sessionDlg");
const cfgSaved = store.get<Partial<SessionCfg>>("cfg", {});
const syncReal = () => document.querySelectorAll<HTMLElement>(".real").forEach((el) => { el.hidden = $<HTMLSelectElement>("source").value !== "parquet"; });
$<HTMLSelectElement>("source").value = cfgSaved.source ?? "synthetic";
$<HTMLSelectElement>("inst").value = cfgSaved.instrument ?? "NQ";
$<HTMLInputElement>("startTime").value = cfgSaved.startTime ?? "09:00";
$<HTMLInputElement>("balance").value = String(cfgSaved.balance ?? 50000);
$<HTMLInputElement>("start").value = cfgSaved.start ?? "";
$<HTMLInputElement>("end").value = cfgSaved.end ?? "";
syncReal();
$("source").onchange = syncReal;
$("newSession").onclick = () => { $("dlgErr").textContent = ""; dlg.showModal(); };
$("dlgCancel").onclick = () => dlg.close();
const readCfg = (): SessionCfg => ({
  source: $<HTMLSelectElement>("source").value, instrument: $<HTMLSelectElement>("inst").value,
  startTime: $<HTMLInputElement>("startTime").value || "09:00", balance: Number($<HTMLInputElement>("balance").value) || 50000,
  start: $<HTMLInputElement>("start").value, end: $<HTMLInputElement>("end").value,
});
$("sessionForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cfg = readCfg();
  if (cfg.source === "parquet" && (!cfg.start || !cfg.end)) { $("dlgErr").textContent = "Pick a start and end date for real data."; return; }
  const err = await newSession(cfg);
  if (err) { $("dlgErr").textContent = err; return; }
  store.set("cfg", cfg);
  dlg.close();
});

window.addEventListener("beforeunload", () => ws?.close());
// Vite hot reload re-runs this module: release the old drawing manager's window/document listeners first.
import.meta.hot?.dispose(() => { drawings.destroy(); ws?.close(); });
void newSession(readCfg()).then((err) => { if (err) { setConn(err, "bad"); } });
