import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type { IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter, Time } from "lightweight-charts";
import { hitTest, logicalToTime, timeToLogical, type XY } from "./geometry";
import { drawDrawing, extraPx, projectPts, type Proj } from "./render";
import { COLORS, DEFAULT_STYLE, NEED, TOOLS, type Drawing, type DrawType, type Pt, type Style, type ToolId } from "./types";

export interface ManagerOpts {
  chart: IChartApi;
  series: ISeriesApi<"Candlestick">;
  container: HTMLElement;          // element wrapping the chart canvas; receives pointer events
  toolbar: HTMLElement;
  propbar: HTMLElement;
  textInput: HTMLInputElement;
  getTimes: () => number[];        // live bar times of the series (data order)
  getInterval: () => number;       // seconds per bar of the current timeframe
  getPointValue: () => number;
  fmtPrice: (p: number) => string;
}

interface Drag { kind: "handle" | "move"; id: string; index: number; startLogical: number; startPrice: number; orig: Drawing; moved: boolean }

const HIT_TOL = 6;
const MAX_UNDO = 60;

export class DrawingManager {
  private drawings: Drawing[] = [];
  private selectedId: string | null = null;
  private tool: ToolId = "cursor";
  private draft: { type: DrawType; pts: Pt[] } | null = null;
  private hoverPt: Pt | null = null;
  private downXY: XY | null = null;
  private brushing = false;
  private drag: Drag | null = null;
  private undo: string[] = [];
  private visible = true;
  private style: Style = { ...DEFAULT_STYLE };
  private storageKey = "";
  private editingId: string | null = null;
  private requestUpdate: (() => void) | null = null;
  private readonly primitive: ISeriesPrimitive<Time>;
  private readonly view: IPrimitivePaneView;

  constructor(private o: ManagerOpts) {
    const renderer: IPrimitivePaneRenderer = {
      draw: (target: CanvasRenderingTarget2D) =>
        target.useMediaCoordinateSpace(({ context, mediaSize }) => this.paint(context, mediaSize.width, mediaSize.height)),
    };
    this.view = { zOrder: () => "top", renderer: () => renderer };
    const views = [this.view];
    this.primitive = {
      attached: (p: SeriesAttachedParameter<Time>) => { this.requestUpdate = p.requestUpdate; },
      detached: () => { this.requestUpdate = null; },
      paneViews: () => views,
    };
    o.series.attachPrimitive(this.primitive);
    this.buildToolbar();
    this.bind();
    this.syncUi();
  }

  // ------------------------------------------------------------------ persistence
  /** Switch the drawing set (e.g. when the session's data source/contract changes). */
  load(key: string): void {
    this.storageKey = key;
    this.selectedId = null; this.draft = null; this.undo = [];
    try { this.drawings = JSON.parse(localStorage.getItem(key) ?? "[]") as Drawing[]; } catch { this.drawings = []; }
    if (!Array.isArray(this.drawings)) this.drawings = [];
    this.syncUi(); this.redraw();
  }
  private save(): void {
    if (!this.storageKey) return;
    try { localStorage.setItem(this.storageKey, JSON.stringify(this.drawings)); } catch { /* quota / private mode */ }
  }
  private pushUndo(): void { this.undo.push(JSON.stringify(this.drawings)); if (this.undo.length > MAX_UNDO) this.undo.shift(); }
  private doUndo(): void {
    const prev = this.undo.pop();
    if (prev === undefined) return;
    this.drawings = JSON.parse(prev) as Drawing[];
    if (!this.drawings.some((d) => d.id === this.selectedId)) this.selectedId = null;
    this.save(); this.syncUi(); this.redraw();
  }

  redraw(): void { this.requestUpdate?.(); }

  // ------------------------------------------------------------------ coordinates
  private times(): number[] { return this.o.getTimes(); }
  private proj(W: number, H: number): Proj {
    const times = this.times(), iv = this.o.getInterval();
    const ts = this.o.chart.timeScale();
    return {
      x: (t) => ts.logicalToCoordinate(timeToLogical(times, t, iv) as never) ?? NaN,
      y: (p) => this.o.series.priceToCoordinate(p) ?? NaN,
      W, H, pointValue: this.o.getPointValue(),
      barsBetween: (a, b) => Math.abs(timeToLogical(times, b, iv) - timeToLogical(times, a, iv)),
      fmtPrice: this.o.fmtPrice,
    };
  }
  private paneSize(): { w: number; h: number } {
    const c = this.o.container;
    return { w: c.clientWidth - this.o.chart.priceScale("right").width(), h: c.clientHeight - this.o.chart.timeScale().height() };
  }
  /** Mouse event -> pixel + data coordinates, or null when outside the plotting pane (axes etc.). */
  private mouse(e: MouseEvent): { xy: XY; pt: Pt; logical: number } | null {
    const r = this.o.container.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const { w, h } = this.paneSize();
    if (x < 0 || y < 0 || x > w || y > h) return null;
    const logical = this.o.chart.timeScale().coordinateToLogical(x);
    const price = this.o.series.coordinateToPrice(y);
    if (logical === null || price === null) return null;
    return { xy: { x, y }, pt: { t: logicalToTime(this.times(), logical, this.o.getInterval()), p: price }, logical };
  }

  // ------------------------------------------------------------------ painting
  private current(): Drawing[] {
    if (!this.draft) return this.drawings;
    const pts = this.hoverPt && this.draft.type !== "brush" ? [...this.draft.pts, this.hoverPt] : this.draft.pts;
    if (pts.length === 0) return this.drawings;
    return [...this.drawings, { id: "draft", type: this.draft.type, pts, style: { ...this.style, dash: true }, text: "" }];
  }
  private paint(ctx: CanvasRenderingContext2D, W: number, H: number): void {
    if (!this.visible) return;
    const P = this.proj(W, H);
    for (const d of this.current()) drawDrawing(ctx, d, P, d.id === this.selectedId);
  }

  // ------------------------------------------------------------------ hit testing / selection
  private hitAt(xy: XY): { d: Drawing; kind: "body" | "handle"; index: number } | null {
    if (!this.visible) return null;
    const { w, h } = this.paneSize();
    const P = this.proj(w, h);
    for (let i = this.drawings.length - 1; i >= 0; i--) {
      const d = this.drawings[i];
      const pts = projectPts(d, P);
      if (pts.some((p) => !Number.isFinite(p.x) || !Number.isFinite(p.y))) continue;
      // only the selected drawing exposes handles; others are body-hit only
      const hit = hitTest(d, pts, xy, w, h, HIT_TOL, extraPx(d, P));
      if (hit && (hit.kind === "body" || d.id === this.selectedId)) return { d, ...hit };
      if (hit && hit.kind === "handle") return { d, kind: "body", index: -1 };
    }
    return null;
  }
  private select(id: string | null): void { this.selectedId = id; this.syncUi(); this.redraw(); }
  private selected(): Drawing | undefined { return this.drawings.find((d) => d.id === this.selectedId); }

  // ------------------------------------------------------------------ pointer handling
  private bind(): void {
    const c = this.o.container;
    c.addEventListener("mousedown", (e) => this.onDown(e), { capture: true });
    window.addEventListener("mousemove", (e) => this.onMove(e));
    window.addEventListener("mouseup", (e) => this.onUp(e));
    c.addEventListener("dblclick", (e) => this.onDouble(e));
    document.addEventListener("keydown", (e) => this.onKey(e));
    this.o.textInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.commitText(true);
      else if (e.key === "Escape") this.commitText(false);
      e.stopPropagation();
    });
    this.o.textInput.addEventListener("blur", () => this.commitText(true));
  }

  private onDown(e: MouseEvent): void {
    if (e.button !== 0 || e.target === this.o.textInput) return;
    const m = this.mouse(e);
    if (!m) return;                                   // axis area: let the chart handle it
    if (this.tool !== "cursor") { e.stopPropagation(); e.preventDefault(); this.toolDown(m); return; }
    const hit = this.hitAt(m.xy);
    if (!hit) { if (this.selectedId) this.select(null); return; }        // empty space: let the chart pan
    e.stopPropagation(); e.preventDefault();
    this.select(hit.d.id);
    this.drag = { kind: hit.kind === "body" ? "move" : "handle", id: hit.d.id, index: hit.index, startLogical: m.logical, startPrice: m.pt.p, orig: JSON.parse(JSON.stringify(hit.d)) as Drawing, moved: false };
  }

  private toolDown(m: { xy: XY; pt: Pt }): void {
    const type = this.tool as DrawType;
    this.downXY = m.xy;
    if (type === "brush") { this.draft = { type, pts: [m.pt] }; this.brushing = true; this.redraw(); return; }
    if (!this.draft) this.draft = { type, pts: [] };
    this.draft.pts.push(m.pt);
    this.hoverPt = null;
    if (this.draft.pts.length >= NEED[type]) this.finish();
    this.redraw();
  }

  private onMove(e: MouseEvent): void {
    const m = this.mouse(e);
    if (this.drag) { if (m) this.dragTo(m); return; }
    if (this.brushing && this.draft && m) {
      const last = this.draft.pts[this.draft.pts.length - 1];
      const P = this.proj(this.paneSize().w, this.paneSize().h);
      if (Math.hypot(P.x(last.t) - m.xy.x, P.y(last.p) - m.xy.y) > 2) { this.draft.pts.push(m.pt); this.redraw(); }
      return;
    }
    if (this.draft) { this.hoverPt = m?.pt ?? this.hoverPt; this.redraw(); return; }
    if (this.tool === "cursor") {
      const over = m ? this.hitAt(m.xy) : null;
      this.o.container.style.cursor = over ? (over.kind === "handle" ? "grab" : "pointer") : "";
    }
  }

  private onUp(e: MouseEvent): void {
    if (this.drag) {
      if (this.drag.moved) this.save();
      this.drag = null; return;
    }
    if (this.brushing) {
      this.brushing = false;
      if (this.draft && this.draft.pts.length > 1) this.finish(); else this.draft = null;
      this.redraw(); return;
    }
    // press-drag-release creates a 2-point drawing in one gesture
    if (this.draft && this.draft.pts.length === 1 && this.downXY) {
      const m = this.mouse(e);
      if (m && Math.hypot(m.xy.x - this.downXY.x, m.xy.y - this.downXY.y) > 6) {
        this.draft.pts.push(m.pt);
        if (this.draft.pts.length >= NEED[this.draft.type]) this.finish();
        this.redraw();
      }
    }
  }

  private dragTo(m: { pt: Pt; logical: number }): void {
    const dr = this.drag!;
    const d = this.drawings.find((x) => x.id === dr.id);
    if (!d) return;
    if (!dr.moved) { this.undo.push(JSON.stringify(this.drawings.map((x) => (x.id === dr.id ? dr.orig : x)))); dr.moved = true; }
    const times = this.times(), iv = this.o.getInterval();
    if (dr.kind === "move") {
      const dl = m.logical - dr.startLogical, dp = m.pt.p - dr.startPrice;
      d.pts = dr.orig.pts.map((p) => ({ t: logicalToTime(times, timeToLogical(times, p.t, iv) + dl, iv), p: p.p + dp }));
      if (dr.orig.stop !== undefined) d.stop = dr.orig.stop + dp;
    } else if ((d.type === "long" || d.type === "short") && dr.index === 2) {
      d.stop = m.pt.p;
    } else if (d.type === "hline" || d.type === "hray") {
      d.pts[0] = { t: d.type === "hray" ? m.pt.t : d.pts[0].t, p: m.pt.p };
    } else if (d.type === "vline") {
      d.pts[0] = { t: m.pt.t, p: d.pts[0].p };
    } else if (dr.index >= 0 && dr.index < d.pts.length) {
      d.pts[dr.index] = m.pt;
    }
    this.redraw();
  }

  private onDouble(e: MouseEvent): void {
    if (this.tool !== "cursor") return;
    const m = this.mouse(e);
    const hit = m ? this.hitAt(m.xy) : null;
    if (hit && hit.d.type === "text") this.openText(hit.d.id, hit.d.text ?? "");
  }

  private onKey(e: KeyboardEvent): void {
    const t = e.target as HTMLElement;
    if (t.matches("input,select,textarea")) return;
    if (e.key === "Escape") { this.draft = null; this.brushing = false; this.setTool("cursor"); this.select(null); }
    else if ((e.key === "Delete" || e.key === "Backspace") && this.selectedId) { e.preventDefault(); this.remove(this.selectedId); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); this.doUndo(); }
  }

  // ------------------------------------------------------------------ mutations
  private finish(): void {
    const dr = this.draft;
    if (!dr) return;
    this.draft = null; this.hoverPt = null; this.brushing = false;
    const d: Drawing = { id: Math.random().toString(36).slice(2, 10), type: dr.type, pts: dr.pts, style: { ...this.style } };
    if (d.type === "long" || d.type === "short") d.stop = d.pts[0].p - (d.pts[1].p - d.pts[0].p);   // mirrored 1:1 until dragged
    if (d.type === "text") d.text = "";
    this.pushUndo();
    this.drawings.push(d);
    this.setTool("cursor");
    this.select(d.id);
    this.save();
    if (d.type === "text") this.openText(d.id, "");
  }
  private remove(id: string): void {
    this.pushUndo();
    this.drawings = this.drawings.filter((d) => d.id !== id);
    if (this.selectedId === id) this.selectedId = null;
    this.save(); this.syncUi(); this.redraw();
  }
  private clearAll(): void {
    if (!this.drawings.length) return;
    if (!window.confirm(`Delete all ${this.drawings.length} drawings?`)) return;
    this.pushUndo(); this.drawings = []; this.selectedId = null;
    this.save(); this.syncUi(); this.redraw();
  }
  private patchStyle(p: Partial<Style>): void {
    const d = this.selected();
    if (d) { this.pushUndo(); d.style = { ...d.style, ...p }; this.save(); }
    this.style = { ...this.style, ...p };      // also becomes the default for the next drawing
    this.syncUi(); this.redraw();
  }

  // ------------------------------------------------------------------ text tool
  private openText(id: string, value: string): void {
    const d = this.drawings.find((x) => x.id === id);
    if (!d) return;
    const P = this.proj(this.paneSize().w, this.paneSize().h);
    const inp = this.o.textInput;
    this.editingId = id;
    inp.value = value; inp.hidden = false;
    inp.style.left = `${P.x(d.pts[0].t)}px`; inp.style.top = `${P.y(d.pts[0].p) - 14}px`;
    setTimeout(() => inp.focus(), 0);
  }
  private commitText(save: boolean): void {
    const inp = this.o.textInput, id = this.editingId;
    if (!id || inp.hidden) return;
    this.editingId = null; inp.hidden = true;
    const d = this.drawings.find((x) => x.id === id);
    if (!d) return;
    const v = inp.value.trim();
    if (save && v) { d.text = v; this.save(); }
    else if (!d.text) { this.drawings = this.drawings.filter((x) => x.id !== id); this.selectedId = null; this.save(); }
    this.syncUi(); this.redraw();
  }

  // ------------------------------------------------------------------ UI (toolbar + property bar)
  setTool(t: ToolId): void {
    this.tool = t; this.draft = null; this.hoverPt = null; this.brushing = false;
    this.o.container.style.cursor = t === "cursor" ? "" : "crosshair";
    this.syncUi(); this.redraw();
  }
  private buildToolbar(): void {
    const tb = this.o.toolbar;
    tb.innerHTML = TOOLS.map((t) => `<button data-tool="${t.id}" title="${t.label}" aria-label="${t.label}">${t.icon}</button>`).join("") +
      `<span class="hsep"></span>` +
      `<button data-act="eye" title="Show / hide drawings" aria-label="Show or hide drawings">${EYE}</button>` +
      `<button data-act="trash" title="Delete all drawings" aria-label="Delete all drawings">${TRASH}</button>`;
    tb.addEventListener("click", (e) => {
      const b = (e.target as HTMLElement).closest("button");
      if (!b) return;
      if (b.dataset.tool) this.setTool(b.dataset.tool as ToolId);
      else if (b.dataset.act === "eye") { this.visible = !this.visible; this.syncUi(); this.redraw(); }
      else if (b.dataset.act === "trash") this.clearAll();
    });
  }
  private syncUi(): void {
    this.o.toolbar.querySelectorAll<HTMLButtonElement>("button[data-tool]").forEach((b) => b.classList.toggle("active", b.dataset.tool === this.tool));
    this.o.toolbar.querySelector('[data-act="eye"]')?.classList.toggle("active", !this.visible);
    const sel = this.selected();
    const pb = this.o.propbar;
    const show = !!sel || this.tool !== "cursor";
    pb.hidden = !show;
    if (!show) return;
    const st = sel?.style ?? this.style;
    pb.innerHTML =
      COLORS.map((c) => `<button class="sw${c === st.color ? " on" : ""}" data-color="${c}" style="background:${c}" aria-label="Color ${c}"></button>`).join("") +
      `<select data-width aria-label="Line width">${[1, 2, 3, 4].map((w) => `<option value="${w}"${w === st.width ? " selected" : ""}>${w}px</option>`).join("")}</select>` +
      `<button data-dash class="${st.dash ? "on" : ""}" title="Dashed" aria-label="Dashed">╌</button>` +
      (sel ? `<button data-del class="danger" title="Delete (Del)" aria-label="Delete drawing">Delete</button>` : "");
    pb.onclick = (e) => {
      const t = (e.target as HTMLElement).closest("button");
      if (!t) return;
      if (t.dataset.color) this.patchStyle({ color: t.dataset.color });
      else if (t.hasAttribute("data-dash")) this.patchStyle({ dash: !st.dash });
      else if (t.hasAttribute("data-del") && this.selectedId) this.remove(this.selectedId);
    };
    pb.onchange = (e) => { const s = e.target as HTMLSelectElement; if (s.hasAttribute("data-width")) this.patchStyle({ width: Number(s.value) }); };
  }
}

const EYE = `<svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 10s3-5.5 8-5.5S18 10 18 10s-3 5.5-8 5.5S2 10 2 10z"/><circle cx="10" cy="10" r="2.3"/></svg>`;
const TRASH = `<svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h12M8 6V4h4v2M6 6l.7 10h6.6L14 6"/></svg>`;
