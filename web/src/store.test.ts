import { describe, expect, it } from "vitest";
import { apply, bracket, drawsChart, initial, MAX_BARS, nextCandleEnd, prevCandleEnd, type Bar, type ServerMsg } from "./store";

const bar = (t: number, c = 100): Bar => ({ t, o: c, h: c + 1, l: c - 1, c, v: 1 });
const acct = { balance: 1, equity: 1, margin: 0, unrealized: 0, stats: { trades: 0 } };
const reset = (): ServerMsg => ({ type: "reset", tf: 60, now: 120, cursor: 120, total: 1000, state: "paused", speed: 1,
  history: { "60": [bar(0), bar(60)] }, forming: { "60": bar(120) }, positions: [], orders: [], trades: [],
  account: acct, sessions: { asia: { hi: null, lo: null } } });
const tick = (closed: Record<string, Bar[]>, extra: Partial<Extract<ServerMsg, { type: "tick" }>> = {}): ServerMsg =>
  ({ type: "tick", tf: 60, now: 180, cursor: 180, total: 1000, bar: bar(179), closed, forming: { "60": bar(180) },
     positions: [], orders: [], new_trades: [], account: acct, sessions: { asia: { hi: 5, lo: 1 } }, ...extra });

describe("store", () => {
  it("reset replaces history", () => {
    const s = apply(initial(), reset());
    expect(s.bars["60"]).toHaveLength(2);
    expect(s.cursor).toBe(120);
  });
  it("tick appends closed bars once and ignores duplicates", () => {
    const s = apply(initial(), reset());
    apply(s, tick({ "60": [bar(120)] }));
    apply(s, tick({ "60": [bar(120)] }));
    expect(s.bars["60"].map(b => b.t)).toEqual([0, 60, 120]);
    expect(s.forming["60"]?.t).toBe(180);
  });
  it("tick accumulates trades and updates overlays", () => {
    const s = apply(initial(), reset());
    const t = { side: "long" as const, qty: 1, entry: 1, exit: 2, pnl: 20, r: 1, opened: 1, closed: 2, reason: "tp" };
    apply(s, tick({}, { new_trades: [t] }));
    apply(s, tick({}, { new_trades: [t] }));
    expect(s.trades).toHaveLength(2);
    expect(s.sessions?.asia).toEqual({ hi: 5, lo: 1 });
  });
  it("reset after step-back discards later bars", () => {
    const s = apply(initial(), reset());
    apply(s, tick({ "60": [bar(120)] }));
    apply(s, reset());
    expect(s.bars["60"]).toHaveLength(2);
  });
  it("tracks playback state, errors and end", () => {
    const s = initial();
    apply(s, { type: "state", state: "playing", speed: 10 });
    expect([s.playback, s.speed]).toEqual(["playing", 10]);
    apply(s, { type: "error", error: "x" });
    expect(s.lastError).toBe("x");
    apply(s, { type: "order_ack", order_id: 1, status: "rejected" });
    expect(s.lastError).toMatch(/rejected/);
    apply(s, { type: "end", account: acct });
    expect([s.ended, s.playback]).toEqual([true, "paused"]);
  });
  it("candle boundaries step by exactly one candle", () => {
    expect(nextCandleEnd(130, 60)).toBe(180);
    expect(nextCandleEnd(120, 60)).toBe(180);   // already on a boundary: go to the next one
    expect(prevCandleEnd(130, 60)).toBe(120);
    expect(prevCandleEnd(120, 60)).toBe(60);
    expect(nextCandleEnd(7, 1)).toBe(8);
  });
  it("view message replaces the bars with the new timeframe's only", () => {
    const s = apply(initial(), reset());
    apply(s, { type: "view", tf: 300, history: { "300": [bar(0), bar(300)] }, forming: { "300": bar(600) } });
    expect(Object.keys(s.bars)).toEqual(["300"]);
    expect(s.bars["300"]).toHaveLength(2);
    expect(s.forming["60"]).toBeUndefined();
    expect(s.forming["300"]?.t).toBe(600);
  });
  it("ticks only touch the timeframes they carry", () => {
    const s = apply(initial(), reset());
    apply(s, { type: "view", tf: 300, history: { "300": [bar(0)] }, forming: { "300": bar(300) } });
    apply(s, tick({ "300": [bar(300)] }, { forming: { "300": bar(600) } }));
    apply(s, tick({}, { forming: { "300": bar(600, 105) } }));
    expect(s.bars["300"].map((b) => b.t)).toEqual([0, 300]);
    expect(s.forming["300"]?.c).toBe(105);
    expect(s.bars["60"]).toBeUndefined();
  });
  it("only messages for the current timeframe repaint the chart; stale ones still update the account", () => {
    const viewing = 300;
    expect(drawsChart(tick({}), 60)).toBe(true);
    expect(drawsChart(tick({}), viewing)).toBe(false);                        // in flight when the user switched 60 -> 300
    expect(drawsChart(reset(), viewing)).toBe(false);
    expect(drawsChart({ type: "view", tf: 300, history: {}, forming: {} }, viewing)).toBe(true);
    expect(drawsChart({ type: "view", tf: 60, history: {}, forming: {} }, viewing)).toBe(false);   // stale view reply
    expect(drawsChart({ type: "state", state: "playing", speed: 1 }, viewing)).toBe(false);
    expect(drawsChart({ type: "error", error: "x" }, viewing)).toBe(false);
    const s = apply(initial(), reset());                                       // ...but apply() still takes their account data
    apply(s, tick({}, { account: { ...acct, equity: 777 } }));
    expect(s.account?.equity).toBe(777);
  });
  it("trims very long histories", () => {
    const s = apply(initial(), reset());
    const many = Array.from({ length: MAX_BARS + 50 }, (_, i) => bar(1000 + i * 60));
    apply(s, tick({ "60": many }));
    expect(s.bars["60"]).toHaveLength(MAX_BARS);
    expect(s.bars["60"].at(-1)?.t).toBe(1000 + (MAX_BARS + 49) * 60);
  });
  it("bracket math is side-aware", () => {
    expect(bracket("long", 100, 10, 20)).toEqual({ sl: 90, tp: 120 });
    expect(bracket("short", 100, 10, 20)).toEqual({ sl: 110, tp: 80 });
    expect(bracket("long", 100, null, null)).toEqual({ sl: null, tp: null });
  });
});
