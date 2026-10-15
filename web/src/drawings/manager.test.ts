// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DrawingManager } from "./manager";
import { COLORS, TOOLS, type Drawing } from "./types";

// ---------------------------------------------------------------- fake chart: a linear pixel <-> data mapping
const W = 800, H = 470, PRICE_AXIS = 60, TIME_AXIS = 30;
const PX_BAR = 10, PX_PT = 2, P_TOP = 20100;          // x = bar*10, y = (20100 - price)*2
const T0 = 1_000_000, IV = 60;
const T = (bar: number) => T0 + bar * IV;              // time of bar index `bar`
const price = (y: number) => P_TOP - y / PX_PT;
const KEY = "drawings:test";

interface Rig {
  m: DrawingManager; container: HTMLElement; toolbar: HTMLElement; propbar: HTMLElement; textInput: HTMLInputElement;
  canvas: HTMLElement; prim: any; requestUpdate: ReturnType<typeof vi.fn>; notify: ReturnType<typeof vi.fn>;
  detachPrimitive: ReturnType<typeof vi.fn>;
}

function rig(nBars = 100): Rig {
  document.body.innerHTML = "";
  const container = document.createElement("div");
  Object.defineProperty(container, "clientWidth", { value: W + PRICE_AXIS });
  Object.defineProperty(container, "clientHeight", { value: H + TIME_AXIS });
  const canvas = document.createElement("canvas");
  container.appendChild(canvas);
  const toolbar = document.createElement("div");
  const propbar = document.createElement("div");
  const textInput = document.createElement("input");
  textInput.hidden = true;
  document.body.append(container, toolbar, propbar, textInput);
  const times = Array.from({ length: nBars }, (_, i) => T(i));
  const chart = {
    timeScale: () => ({ logicalToCoordinate: (l: number) => l * PX_BAR, coordinateToLogical: (x: number) => x / PX_BAR, height: () => TIME_AXIS }),
    priceScale: () => ({ width: () => PRICE_AXIS }),
  };
  let prim: any = null;
  const detachPrimitive = vi.fn();
  const series = { attachPrimitive: (p: unknown) => { prim = p; }, detachPrimitive, priceToCoordinate: (p: number) => (P_TOP - p) * PX_PT, coordinateToPrice: (y: number) => price(y) };
  const requestUpdate = vi.fn(), notify = vi.fn();
  const m = new DrawingManager({
    chart: chart as never, series: series as never, container, toolbar, propbar, textInput,
    getTimes: () => times, getInterval: () => IV, getPointValue: () => 20, fmtPrice: (p) => p.toFixed(2), notify,
  });
  prim.attached({ requestUpdate });
  m.load(KEY);
  return { m, container, toolbar, propbar, textInput, canvas, prim, requestUpdate, notify, detachPrimitive };
}

// ---------------------------------------------------------------- input helpers
function pe(kind: string, x: number, y: number, o: { type?: string; primary?: boolean; button?: number; ts?: number } = {}): MouseEvent {
  const ev = new MouseEvent(kind, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: o.button ?? 0 });
  Object.defineProperties(ev, { pointerType: { value: o.type ?? "mouse" }, isPrimary: { value: o.primary ?? true }, pointerId: { value: 1 } });
  if (o.ts !== undefined) Object.defineProperty(ev, "timeStamp", { value: o.ts });
  return ev;
}
type O = Parameters<typeof pe>[3];
let r: Rig;
const down = (x: number, y: number, o?: O) => r.container.dispatchEvent(pe("pointerdown", x, y, o));
const move = (x: number, y: number, o?: O) => window.dispatchEvent(pe("pointermove", x, y, o));
const up = (x: number, y: number, o?: O) => window.dispatchEvent(pe("pointerup", x, y, o));
const click = (x: number, y: number, o?: O) => { down(x, y, o); up(x, y, o); };
const tool = (id: string) => (r.toolbar.querySelector(`[data-tool="${id}"]`) as HTMLButtonElement).click();
const key = (k: string, extra: KeyboardEventInit = {}) => document.body.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, ...extra }));
const undo = () => key("z", { ctrlKey: true });
const saved = (): Drawing[] => JSON.parse(localStorage.getItem(KEY) ?? "[]");
const selected = () => !r.propbar.hidden && !!r.propbar.querySelector("[data-del]");
const settle = () => vi.advanceTimersByTimeAsync(80);       // lets the post-gesture "claim" release fire

function paint(): string[] {
  const calls: string[] = [];
  const ctx = new Proxy({}, { get: (_t, k) => (k === "measureText" ? (s: string) => ({ width: s.length * 6 }) : (..._a: unknown[]) => { calls.push(String(k)); }), set: () => true });
  r.prim.paneViews()[0].renderer().draw({ useMediaCoordinateSpace: (cb: any) => cb({ context: ctx, mediaSize: { width: W, height: H } }) });
  return calls;
}

const rigs: Rig[] = [];
const newRig = (n?: number) => { const x = rig(n); rigs.push(x); return x; };

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16));
  localStorage.clear();
  r = newRig();
});
afterEach(() => {
  while (rigs.length) rigs.pop()!.m.destroy();       // managers listen on window/document: never leak them across tests
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear();
});

// ---------------------------------------------------------------- toolbar + creation
describe("toolbar", () => {
  it("renders every tool plus show/hide and clear-all, with cursor active", () => {
    const ids = Array.from(r.toolbar.querySelectorAll<HTMLElement>("[data-tool]")).map((b) => b.dataset.tool);
    expect(ids).toEqual(TOOLS.map((t) => t.id));
    expect(r.toolbar.querySelector("[data-act=eye]")).toBeTruthy();
    expect(r.toolbar.querySelector("[data-act=trash]")).toBeTruthy();
    expect(r.toolbar.querySelector("[data-tool=cursor]")!.classList.contains("active")).toBe(true);
    expect(r.container.style.touchAction).toBe("none");
  });
  it("picking a tool activates it and sets a crosshair cursor", () => {
    tool("trend");
    expect(r.toolbar.querySelector("[data-tool=trend]")!.classList.contains("active")).toBe(true);
    expect(r.container.style.cursor).toBe("crosshair");
    tool("cursor");
    expect(r.container.style.cursor).toBe("");
  });
});

describe("creating drawings", () => {
  it("trend line by click-click, anchored in time and price", () => {
    tool("trend"); click(100, 100); move(200, 150); click(200, 150);
    const [d] = saved();
    expect(d.type).toBe("trend");
    expect(d.pts).toEqual([{ t: T(10), p: 20050 }, { t: T(20), p: 20025 }]);
  });
  it("rectangle by press-drag-release in one gesture", () => {
    tool("rect"); down(300, 100); move(350, 150); up(400, 200);
    expect(saved()[0].pts).toEqual([{ t: T(30), p: 20050 }, { t: T(40), p: 20000 }]);
  });
  it("one-click tools finish immediately", () => {
    for (const [id, x] of [["hline", 100], ["hray", 150], ["vline", 200]] as const) { tool(id); click(x, 100); }
    expect(saved().map((d) => d.type)).toEqual(["hline", "hray", "vline"]);
  });
  it("parallel channel needs three points", () => {
    tool("channel"); click(100, 300); click(200, 250);
    expect(saved()).toHaveLength(0);                     // still drafting
    click(150, 350);
    expect(saved()[0].pts).toHaveLength(3);
  });
  it("channel can start with a drag then finish with a click", () => {
    tool("channel"); down(100, 300); move(150, 280); up(200, 250);
    expect(saved()).toHaveLength(0);
    click(150, 350);
    expect(saved()[0].pts).toHaveLength(3);
  });
  it("long position mirrors the stop 1:1 until dragged; short works downward", () => {
    tool("long"); click(100, 100); click(200, 60);
    let d = saved()[0];
    expect(d.pts[0].p).toBe(20050); expect(d.pts[1].p).toBe(20070); expect(d.stop).toBe(20030);
    tool("short"); click(300, 100); click(400, 140);
    d = saved()[1];
    expect(d.type).toBe("short"); expect(d.stop).toBe(20070);
  });
  it("the tool returns to cursor and the new drawing is selected", () => {
    tool("trend"); click(100, 100); click(200, 150);
    expect(r.toolbar.querySelector("[data-tool=cursor]")!.classList.contains("active")).toBe(true);
    expect(selected()).toBe(true);
  });
  it("a second (non-primary) finger never draws", () => {
    tool("hline"); click(100, 100, { primary: false, type: "touch" });
    expect(saved()).toHaveLength(0);
  });
  it("right-click does not draw", () => {
    tool("hline"); click(100, 100, { button: 2 });
    expect(saved()).toHaveLength(0);
  });
  it("clicks on the price/time axis are ignored (the chart keeps them)", () => {
    tool("hline"); click(W + 20, 100); click(100, H + 10);
    expect(saved()).toHaveLength(0);
  });
  it("brush decimates dense input", () => {
    tool("brush"); down(100, 100);
    for (let i = 1; i <= 200; i++) move(100 + i, 100);
    up(300, 100);
    const n = saved()[0].pts.length;
    expect(n).toBeGreaterThan(30); expect(n).toBeLessThan(70);       // ~1 point per 4 px, not 200
  });
  it("brush is capped at 1500 points and finishes itself", () => {
    tool("brush"); down(100, 100);
    for (let i = 1; i <= 1700; i++) move(i % 2 ? 150 : 100, 100);
    up(100, 100);
    expect(saved()[0].pts).toHaveLength(1500);
    expect(saved()).toHaveLength(1);
  });
  it("a 1-point brush tap is discarded", () => {
    tool("brush"); click(100, 100);
    expect(saved()).toHaveLength(0);
  });
  it("Escape abandons a half-drawn shape and returns to the cursor", () => {
    tool("trend"); click(100, 100);
    key("Escape");
    click(300, 300); click(400, 350);
    expect(saved()).toHaveLength(0);
    expect(r.toolbar.querySelector("[data-tool=cursor]")!.classList.contains("active")).toBe(true);
  });
  it("points beyond the last bar get an extrapolated future time (replay has no future data)", () => {
    r.m.destroy(); r = newRig(40);                      // last bar index 39 => x = 390
    tool("vline"); click(600, 100);                     // logical 60 => 21 bars after the last bar
    expect(saved()[0].pts[0].t).toBe(T(39) + 21 * IV);
  });
});

// ---------------------------------------------------------------- select / move / handles / delete / undo
describe("selecting and editing", () => {
  const trend = () => { tool("trend"); click(100, 100); click(200, 150); };      // ends selected
  const deselect = () => { click(700, 20); };                                    // empty space

  it("tapping empty space deselects; tapping the line selects it again", () => {
    trend(); deselect();
    expect(selected()).toBe(false);
    click(150, 125);
    expect(selected()).toBe(true);
  });
  it("dragging the body moves every point by the same bars and price", async () => {
    trend(); deselect();
    down(150, 125); move(165, 135); move(180, 145); up(180, 145);
    expect(saved()[0].pts).toEqual([{ t: T(13), p: 20040 }, { t: T(23), p: 20015 }]);    // +3 bars, -10 pts
  });
  it("dragging a handle moves only that point", () => {
    trend();                                            // selected: handles live
    down(200, 150); move(240, 130); up(260, 100);
    expect(saved()[0].pts).toEqual([{ t: T(10), p: 20050 }, { t: T(26), p: 20050 }]);
  });
  it("horizontal line handle changes only the price", () => {
    tool("hline"); click(400, 200);
    down(400, 200); move(450, 220); up(500, 240);
    expect(saved()[0].pts).toEqual([{ t: T(40), p: 19980 }]);
  });
  it("vertical line handle changes only the time", () => {
    tool("vline"); click(400, 200);
    down(400, 200); move(450, 220); up(500, 240);
    expect(saved()[0].pts).toEqual([{ t: T(50), p: 20000 }]);
  });
  it("position tool: the stop handle moves the stop only", () => {
    tool("long"); click(100, 100); click(200, 60);          // entry 20050 target 20070 stop 20030 (handle at 150,140)
    down(150, 140); move(150, 160); up(150, 180);
    const d = saved()[0];
    expect(d.stop).toBe(20010);
    expect(d.pts[0].p).toBe(20050); expect(d.pts[1].p).toBe(20070);
  });
  it("moving a position tool shifts entry, target and stop together", () => {
    tool("long"); click(100, 100); click(200, 60);
    deselect();
    down(120, 70); move(120, 80); up(120, 90);            // inside the profit box, drag +20px down => -10 pts
    const d = saved()[0];
    expect([d.pts[0].p, d.pts[1].p, d.stop]).toEqual([20040, 20060, 20020]);
  });
  it("Delete removes the selection; Ctrl+Z brings it back", () => {
    trend();
    key("Delete");
    expect(saved()).toHaveLength(0);
    undo();
    expect(saved()).toHaveLength(1);
  });
  it("Ctrl+Z undoes a move, and a creation", () => {
    trend(); deselect();
    down(150, 125); move(180, 145); up(180, 145);
    expect(saved()[0].pts[0].t).toBe(T(13));
    undo();
    expect(saved()[0].pts[0].t).toBe(T(10));
    undo();
    expect(saved()).toHaveLength(0);
  });
  it("the release position counts even without a final move event (a finger can lift elsewhere)", () => {
    trend(); deselect();
    down(150, 125); move(160, 130);                      // drag begins...
    up(180, 145);                                        // ...and ends further along with no move in between
    expect(saved()[0].pts[0]).toEqual({ t: T(13), p: 20040 });
  });
  it("a click without dragging does not create an undo step or rewrite the drawing", () => {
    trend(); deselect();
    const before = localStorage.getItem(KEY);
    click(150, 125);
    expect(localStorage.getItem(KEY)).toBe(before);
    undo();                                              // undoes the creation, not a phantom move
    expect(saved()).toHaveLength(0);
  });
  it("pointercancel mid-drag restores the drawing and leaves no stale undo entry", () => {
    trend(); deselect(); tool("hline"); click(500, 300); deselect();
    const before = JSON.stringify(saved()[0].pts);
    down(150, 125); move(190, 160);
    window.dispatchEvent(new Event("pointercancel"));
    expect(JSON.stringify(saved()[0].pts)).toBe(before);
    undo();                                              // must undo the hline creation, not a ghost of the drag
    expect(saved().map((d) => d.type)).toEqual(["trend"]);
  });
  it("Backspace also deletes; Escape deselects", () => {
    trend(); key("Backspace");
    expect(saved()).toHaveLength(0);
    undo(); key("Escape");
    expect(selected()).toBe(false);
  });
  it("Delete/Ctrl+Z are ignored while a dialog is open or an input has focus", () => {
    trend();
    const dlg = document.createElement("dialog"); dlg.setAttribute("open", ""); document.body.append(dlg);
    key("Delete");
    expect(saved()).toHaveLength(1);
    dlg.remove();
    const inp = document.createElement("input"); document.body.append(inp);
    inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));
    expect(saved()).toHaveLength(1);
    key("Delete");
    expect(saved()).toHaveLength(0);
  });
  it("clear-all (confirmed) empties the chart and is undoable; declined does nothing", () => {
    trend(); tool("hline"); click(300, 300);
    vi.stubGlobal("confirm", vi.fn(() => false));
    (r.toolbar.querySelector("[data-act=trash]") as HTMLButtonElement).click();
    expect(saved()).toHaveLength(2);
    vi.stubGlobal("confirm", vi.fn(() => true));
    (r.toolbar.querySelector("[data-act=trash]") as HTMLButtonElement).click();
    expect(saved()).toHaveLength(0);
    undo();
    expect(saved()).toHaveLength(2);
  });
});

// ---------------------------------------------------------------- touch / pen / chart isolation
describe("pointer types", () => {
  const flatTrend = () => { tool("trend"); click(100, 200); click(300, 200); click(700, 20); };   // deselected at the end
  it("a finger has a larger hit tolerance than a mouse", () => {
    flatTrend();
    click(200, 211, { type: "mouse" });                  // 11 px off: miss
    expect(selected()).toBe(false);
    click(200, 211, { type: "touch" });                  // 14 px tolerance: hit
    expect(selected()).toBe(true);
  });
  it("a pen behaves like a mouse", () => {
    flatTrend();
    click(200, 211, { type: "pen" });
    expect(selected()).toBe(false);
    click(200, 204, { type: "pen" });
    expect(selected()).toBe(true);
  });
  it("touch can draw", () => {
    tool("trend"); down(100, 100, { type: "touch" }); move(150, 120, { type: "touch" }); up(200, 150, { type: "touch" });
    expect(saved()[0].pts).toHaveLength(2);
  });
  it("the chart's own mouse/touch handlers are kept out of a gesture we own, and see ordinary ones", async () => {
    const seen: string[] = [];
    r.canvas.addEventListener("mousedown", () => seen.push("mouse"));
    r.canvas.addEventListener("touchstart", () => seen.push("touch"));
    const fire = () => {
      r.canvas.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      r.canvas.dispatchEvent(new Event("touchstart", { bubbles: true, cancelable: true }));
    };
    tool("vline"); down(100, 100);                       // we claim the gesture
    fire();
    expect(seen).toEqual([]);
    up(100, 100); await settle();
    fire();                                              // claim released: the chart gets its events again
    expect(seen).toEqual(["mouse", "touch"]);
    seen.length = 0;
    tool("cursor"); down(700, 20); fire(); up(700, 20);   // empty-space press with the cursor: chart may pan
    expect(seen).toEqual(["mouse", "touch"]);
  });
});

// ---------------------------------------------------------------- text
describe("text tool", () => {
  const typeText = (v: string) => { r.textInput.value = v; };
  const enter = () => r.textInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  const makeText = (v = "Support") => { tool("text"); click(100, 100); typeText(v); enter(); };

  it("opens an editor on click and saves the typed text on Enter", () => {
    tool("text"); click(100, 100);
    expect(r.textInput.hidden).toBe(false);
    typeText("Support"); enter();
    expect(r.textInput.hidden).toBe(true);
    expect(saved()[0]).toMatchObject({ type: "text", text: "Support" });
  });
  it("Escape on a brand-new empty label removes it", () => {
    tool("text"); click(100, 100); typeText("x");
    r.textInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(saved()).toHaveLength(0);
  });
  it("blur commits", () => {
    tool("text"); click(100, 100); typeText("Blur");
    r.textInput.dispatchEvent(new Event("blur"));
    expect(saved()[0].text).toBe("Blur");
  });
  it("pressing on the chart commits an open editor (preventDefault would otherwise keep focus)", () => {
    tool("text"); click(100, 100); typeText("Chart");
    click(700, 20);
    expect(r.textInput.hidden).toBe(true);
    expect(saved()[0].text).toBe("Chart");
  });
  it("double press within 400ms edits an existing label; slower presses do not", () => {
    makeText();
    click(700, 20);
    down(105, 100, { ts: 1000 }); up(105, 100);
    down(105, 100, { ts: 1500 }); up(105, 100);          // 500 ms later: not a double press
    expect(r.textInput.hidden).toBe(true);
    click(700, 20);
    down(105, 100, { ts: 5000 }); up(105, 100);
    down(105, 100, { ts: 5200 }); up(105, 100);          // 200 ms later
    expect(r.textInput.hidden).toBe(false);
    expect(r.textInput.value).toBe("Support");
  });
  it("editing existing text is undoable", () => {
    makeText("Support"); click(700, 20);
    down(105, 100, { ts: 1000 }); up(105, 100); down(105, 100, { ts: 1100 }); up(105, 100);
    typeText("Resistance"); enter();
    expect(saved()[0].text).toBe("Resistance");
    undo();
    expect(saved()[0].text).toBe("Support");
  });
});

// ---------------------------------------------------------------- property bar, visibility, persistence
describe("property bar", () => {
  it("appears for a selection with colours, width, dash and delete", () => {
    tool("trend"); click(100, 100); click(200, 150);
    expect(r.propbar.hidden).toBe(false);
    expect(r.propbar.querySelectorAll(".sw")).toHaveLength(COLORS.length);
    expect(r.propbar.querySelector("[data-width]")).toBeTruthy();
    expect(r.propbar.querySelector("[data-dash]")).toBeTruthy();
    expect(r.propbar.querySelector("[data-del]")).toBeTruthy();
  });
  it("changes colour, width and dash of the selected drawing and persists them", () => {
    tool("trend"); click(100, 100); click(200, 150);
    (r.propbar.querySelectorAll(".sw")[2] as HTMLButtonElement).click();
    const w = r.propbar.querySelector("[data-width]") as HTMLSelectElement; w.value = "4"; w.dispatchEvent(new Event("change", { bubbles: true }));
    (r.propbar.querySelector("[data-dash]") as HTMLButtonElement).click();
    expect(saved()[0].style).toEqual({ color: COLORS[2], width: 4, dash: true });
  });
  it("style changes are undoable", () => {
    tool("trend"); click(100, 100); click(200, 150);
    (r.propbar.querySelectorAll(".sw")[1] as HTMLButtonElement).click();
    undo();
    expect(saved()[0].style.color).not.toBe(COLORS[1]);
  });
  it("with a tool active and nothing selected it sets the default style for the next drawing", () => {
    tool("hline");
    expect(r.propbar.hidden).toBe(false);
    expect(r.propbar.querySelector("[data-del]")).toBeNull();
    (r.propbar.querySelectorAll(".sw")[3] as HTMLButtonElement).click();
    click(100, 100);
    expect(saved()[0].style.color).toBe(COLORS[3]);
  });
  it("is hidden when nothing is selected and the cursor is active", () => {
    tool("trend"); click(100, 100); click(200, 150); click(700, 20);
    expect(r.propbar.hidden).toBe(true);
  });
  it("Delete button removes the drawing", () => {
    tool("trend"); click(100, 100); click(200, 150);
    (r.propbar.querySelector("[data-del]") as HTMLButtonElement).click();
    expect(saved()).toHaveLength(0);
  });
});

describe("visibility and painting", () => {
  it("paints each drawing; hiding paints nothing; picking a tool shows them again", () => {
    tool("trend"); click(100, 100); click(200, 150); click(700, 20);
    expect(paint()).toContain("stroke");
    (r.toolbar.querySelector("[data-act=eye]") as HTMLButtonElement).click();
    expect(paint()).toEqual([]);
    tool("hline");
    expect(r.toolbar.querySelector("[data-act=eye]")!.classList.contains("active")).toBe(false);
    expect(paint().length).toBeGreaterThan(0);
  });
  it("only the selected drawing gets handles", () => {
    tool("trend"); click(100, 100); click(200, 150);
    expect(paint().filter((c) => c === "strokeRect")).toHaveLength(2);     // two end handles
    click(700, 20);
    expect(paint().filter((c) => c === "strokeRect")).toHaveLength(0);
  });
  it("a draft is previewed while drawing (with the hover point)", () => {
    tool("rect"); click(100, 100); move(200, 150);
    expect(paint()).toContain("strokeRect");
  });
  it("requests a chart repaint when drawings change", () => {
    r.requestUpdate.mockClear();
    tool("hline"); click(100, 100);
    expect(r.requestUpdate).toHaveBeenCalled();
  });
  it("paints every tool type without throwing", () => {
    const placements: [string, [number, number][]][] = [
      ["trend", [[100, 100], [200, 150]]], ["ray", [[100, 120], [200, 150]]], ["arrow", [[100, 140], [200, 110]]],
      ["hline", [[100, 100]]], ["hray", [[100, 100]]], ["vline", [[300, 100]]], ["rect", [[100, 100], [200, 150]]],
      ["ellipse", [[100, 100], [200, 150]]], ["channel", [[100, 300], [200, 250], [150, 350]]], ["fib", [[100, 100], [250, 300]]],
      ["long", [[100, 100], [200, 60]]], ["short", [[300, 100], [400, 140]]], ["measure", [[100, 100], [220, 160]]],
    ];
    for (const [id, pts] of placements) { tool(id); for (const [x, y] of pts) click(x, y); }
    tool("text"); click(500, 100); r.textInput.value = "T"; r.textInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    tool("brush"); down(100, 400); move(150, 420); move(200, 400); up(250, 430);
    expect(saved()).toHaveLength(placements.length + 2);
    expect(() => paint()).not.toThrow();
    expect(paint().length).toBeGreaterThan(50);
  });
});

describe("destroy", () => {
  it("detaches from the series and stops reacting to pointer, keyboard and toolbar input", () => {
    tool("trend"); click(100, 100); click(200, 150);
    r.m.destroy();
    expect(r.detachPrimitive).toHaveBeenCalledTimes(1);
    expect(r.container.style.touchAction).toBe("");
    const before = localStorage.getItem(KEY);
    key("Delete"); undo();
    tool("hline"); click(300, 300);                      // toolbar and chart no longer respond
    down(150, 125); move(190, 160); up(190, 160);
    expect(localStorage.getItem(KEY)).toBe(before);
  });
  it("two managers do not interfere once the first is destroyed (hot reload)", () => {
    tool("hline"); click(100, 100);
    r.m.destroy();
    const second = newRig();
    r = second;
    tool("trend"); click(100, 100); click(200, 150);     // the new manager loaded the stored hline, then adds a trend
    expect(saved().map((d) => d.type)).toEqual(["hline", "trend"]);
    undo();                                              // a leaked old manager would also undo and overwrite storage
    expect(saved().map((d) => d.type)).toEqual(["hline"]);
  });
});

describe("persistence", () => {
  it("round-trips through localStorage via load()", () => {
    tool("trend"); click(100, 100); click(200, 150);
    r.m.destroy(); r = newRig();                         // a fresh manager over the same storage
    expect(saved()).toHaveLength(1);
    expect(paint()).toContain("stroke");
  });
  it("drops corrupt stored records instead of crashing the renderer", () => {
    localStorage.setItem(KEY, JSON.stringify([
      { id: "ok", type: "hline", pts: [{ t: T(5), p: 20000 }], style: { color: "#fff", width: 2, dash: false } },
      { type: "trend" }, { type: "trend", pts: [null] }, null, 7, { type: "rect", pts: [{ t: "x", p: 1 }, { t: 2, p: 3 }] },
    ]));
    r.m.load(KEY);
    expect(() => paint()).not.toThrow();
    tool("vline"); click(100, 100);                      // forces a save
    expect(saved().map((d) => d.type)).toEqual(["hline", "vline"]);
  });
  it("garbage JSON in storage is tolerated", () => {
    localStorage.setItem(KEY, "{not json");
    expect(() => r.m.load(KEY)).not.toThrow();
    expect(paint()).toEqual([]);
  });
  it("different keys keep separate drawing sets", () => {
    tool("hline"); click(100, 100);
    r.m.load("drawings:other");
    expect(paint()).toEqual([]);
    r.m.load(KEY);
    expect(paint().length).toBeGreaterThan(0);
  });
  it("tells the user when storage is full instead of failing silently", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("QuotaExceededError"); });
    tool("hline"); click(100, 100);
    expect(r.notify).toHaveBeenCalledWith(expect.stringMatching(/Couldn't save drawings/));
  });
  it("loading a different set clears selection and undo history", () => {
    tool("trend"); click(100, 100); click(200, 150);
    r.m.load("drawings:other");
    expect(selected()).toBe(false);
    undo();
    r.m.load(KEY);
    expect(saved()).toHaveLength(1);                     // the undo above had nothing to revert
  });
});
