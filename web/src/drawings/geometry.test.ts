import { describe, expect, it } from "vitest";
import {
  channelOffsetY,
  distToSegment,
  fibPrice,
  fmtDuration,
  handlePoints,
  hitTest,
  inBox,
  logicalToTime,
  measure,
  positionLevels,
  rayEnd,
  sanitizeDrawings,
  timeToLogical,
} from "./geometry";
import type { Drawing } from "./types";

const times = [100, 160, 220, 280]; // 60s bars
const style = { color: "#fff", width: 2, dash: false };
const mk = (type: Drawing["type"], pts: Drawing["pts"], extra: Partial<Drawing> = {}): Drawing => ({
  id: "x",
  type,
  pts,
  style,
  ...extra,
});

describe("time <-> logical", () => {
  it("is exact on bars and interpolates between them", () => {
    expect(timeToLogical(times, 160, 60)).toBe(1);
    expect(timeToLogical(times, 190, 60)).toBeCloseTo(1.5);
    expect(logicalToTime(times, 1.5, 60)).toBeCloseTo(190);
  });
  it("extrapolates beyond the last bar by the interval (drawing in the empty future)", () => {
    expect(timeToLogical(times, 400, 60)).toBeCloseTo(3 + 2);
    expect(logicalToTime(times, 5, 60)).toBe(280 + 2 * 60);
    expect(timeToLogical(times, 40, 60)).toBeCloseTo(-1);
  });
  it("round-trips across the whole range incl. gaps", () => {
    const gappy = [0, 60, 120, 7200, 7260];
    for (const t of [0, 30, 90, 119, 4000, 7100, 7260, 9999, -500]) {
      expect(logicalToTime(gappy, timeToLogical(gappy, t, 60), 60)).toBeCloseTo(t, 6);
    }
  });
  it("survives empty/one-bar timelines", () => {
    expect(timeToLogical([], 5, 60)).toBe(0);
    expect(logicalToTime([], 3, 60)).toBe(0);
    expect(timeToLogical([100], 160, 60)).toBe(1);
  });
});

describe("2D helpers", () => {
  it("distance to segment clamps at endpoints", () => {
    expect(distToSegment({ x: 5, y: 3 }, { x: 0, y: 0 }, { x: 10, y: 0 })).toBe(3);
    expect(distToSegment({ x: 13, y: 4 }, { x: 0, y: 0 }, { x: 10, y: 0 })).toBe(5);
    expect(distToSegment({ x: 1, y: 1 }, { x: 2, y: 2 }, { x: 2, y: 2 })).toBeCloseTo(Math.SQRT2);
  });
  it("ray leaves the pane on the right edge", () => {
    expect(rayEnd({ x: 0, y: 0 }, { x: 10, y: 5 }, 100, 100)).toEqual({ x: 100, y: 50 });
    expect(rayEnd({ x: 50, y: 50 }, { x: 40, y: 50 }, 100, 100)).toEqual({ x: 0, y: 50 });
    const e = rayEnd({ x: 10, y: 10 }, { x: 10, y: 20 }, 100, 80);
    expect(e).toEqual({ x: 10, y: 80 });
  });
  it("fib 0 is at the end point, 1 at the start", () => {
    expect(fibPrice(100, 200, 0)).toBe(200);
    expect(fibPrice(100, 200, 1)).toBe(100);
    expect(fibPrice(100, 200, 0.5)).toBe(150);
  });
  it("channel offset is the vertical distance to the parallel line", () => {
    expect(channelOffsetY({ x: 0, y: 0 }, { x: 10, y: 10 }, { x: 4, y: 10 })).toBe(6);
    expect(channelOffsetY({ x: 0, y: 0 }, { x: 0, y: 10 }, { x: 4, y: 7 })).toBe(7);
  });
  it("box test", () => {
    expect(inBox({ x: 5, y: 5 }, { x: 10, y: 0 }, { x: 0, y: 10 })).toBe(true);
    expect(inBox({ x: 11, y: 5 }, { x: 0, y: 0 }, { x: 10, y: 10 })).toBe(false);
  });
});

describe("sanitizeDrawings (untrusted localStorage data)", () => {
  const good = {
    id: "a",
    type: "trend",
    pts: [
      { t: 1, p: 2 },
      { t: 3, p: 4 },
    ],
    style: { color: "#fff", width: 2, dash: false },
  };
  it("keeps valid drawings and fills in missing style/id", () => {
    const r = sanitizeDrawings([good, { type: "hline", pts: [{ t: 1, p: 5 }] }]);
    expect(r).toHaveLength(2);
    expect(r[1].style).toEqual({ color: "#2962ff", width: 2, dash: false });
    expect(r[1].id).toMatch(/^[a-z0-9]{1,8}$/);
  });
  it("drops anything that would crash the renderer", () => {
    const bad = [
      null,
      5,
      "x",
      [],
      {},
      { type: "nope", pts: [] },
      { type: "trend" },
      { type: "trend", pts: null },
      { type: "trend", pts: [{ t: 1, p: 2 }] },
      { type: "trend", pts: [{ t: 1, p: 2 }, null] },
      {
        type: "trend",
        pts: [
          { t: "1", p: 2 },
          { t: 3, p: 4 },
        ],
      },
      {
        type: "trend",
        pts: [
          { t: NaN, p: 2 },
          { t: 3, p: 4 },
        ],
      },
      {
        type: "channel",
        pts: [
          { t: 1, p: 1 },
          { t: 2, p: 2 },
        ],
      },
      { type: "brush", pts: [{ t: 1, p: 1 }] },
      { type: "toString", pts: [] },
      { type: "__proto__", pts: [] },
    ];
    expect(sanitizeDrawings(bad)).toEqual([]);
    expect(sanitizeDrawings("garbage")).toEqual([]);
    expect(sanitizeDrawings(undefined)).toEqual([]);
  });
  it("clamps style values and keeps optional fields only when valid", () => {
    const [d] = sanitizeDrawings([
      { ...good, style: { color: 5, width: 999, dash: "yes" }, text: 3, stop: "x" },
    ]);
    expect(d.style).toEqual({ color: "#2962ff", width: 8, dash: false });
    expect("text" in d).toBe(false);
    expect("stop" in d).toBe(false);
    const [e] = sanitizeDrawings([{ ...good, type: "long", text: "hi", stop: 7 }]);
    expect(e.stop).toBe(7);
    expect(e.text).toBe("hi");
  });
  it("does not alias or mutate its input", () => {
    const src = [JSON.parse(JSON.stringify(good))];
    const [d] = sanitizeDrawings(src);
    d.pts[0].t = 999;
    expect(src[0].pts[0].t).toBe(1);
  });
});

describe("trading maths", () => {
  it("long position levels default to 1:1 stop and honor an explicit stop", () => {
    const d = mk("long", [
      { t: 0, p: 100 },
      { t: 1, p: 110 },
    ]);
    expect(positionLevels(d)).toMatchObject({ entry: 100, target: 110, stop: 90, rr: 1 });
    expect(positionLevels({ ...d, stop: 95 })).toMatchObject({
      stop: 95,
      rewardPts: 10,
      riskPts: 5,
      rr: 2,
    });
  });
  it("short position works with target below entry", () => {
    const d = mk(
      "short",
      [
        { t: 0, p: 100 },
        { t: 1, p: 90 },
      ],
      { stop: 105 },
    );
    expect(positionLevels(d).rr).toBe(2);
  });
  it("measure reports change, percent, bars and duration", () => {
    const m = measure({ t: 0, p: 200 }, { t: 3600, p: 210 }, 60);
    expect(m).toEqual({ dPrice: 10, pct: 5, bars: 60, seconds: 3600 });
    expect(fmtDuration(3600)).toBe("1h");
    expect(fmtDuration(90061)).toBe("1d 1h 1m");
    expect(fmtDuration(45)).toBe("45s");
  });
});

describe("hit testing", () => {
  const W = 500,
    H = 300;
  it("trend line: near the line hits, away misses, endpoints are handles", () => {
    const d = mk("trend", [
      { t: 0, p: 0 },
      { t: 1, p: 1 },
    ]);
    const xy = [
      { x: 0, y: 0 },
      { x: 100, y: 100 },
    ];
    expect(hitTest(d, xy, { x: 50, y: 52 }, W, H, 5)).toEqual({ kind: "body", index: -1 });
    expect(hitTest(d, xy, { x: 50, y: 80 }, W, H, 5)).toBeNull();
    expect(hitTest(d, xy, { x: 99, y: 101 }, W, H, 5)).toEqual({ kind: "handle", index: 1 });
  });
  it("rect hits inside; horizontal line hits only near its y", () => {
    const r = mk("rect", [
      { t: 0, p: 0 },
      { t: 1, p: 1 },
    ]);
    expect(
      hitTest(
        r,
        [
          { x: 10, y: 10 },
          { x: 110, y: 60 },
        ],
        { x: 60, y: 30 },
        W,
        H,
        4,
      )?.kind,
    ).toBe("body");
    expect(
      hitTest(
        r,
        [
          { x: 10, y: 10 },
          { x: 110, y: 60 },
        ],
        { x: 200, y: 30 },
        W,
        H,
        4,
      ),
    ).toBeNull();
    const h = mk("hline", [{ t: 0, p: 0 }]);
    expect(hitTest(h, [{ x: 300, y: 120 }], { x: 5, y: 122 }, W, H, 4)?.kind).toBe("body");
    expect(hitTest(h, [{ x: 300, y: 120 }], { x: 5, y: 140 }, W, H, 4)).toBeNull();
  });
  it("hray only extends to the right; vline only near x", () => {
    const h = mk("hray", [{ t: 0, p: 0 }]);
    expect(hitTest(h, [{ x: 100, y: 50 }], { x: 300, y: 50 }, W, H, 4)?.kind).toBe("body");
    expect(hitTest(h, [{ x: 100, y: 50 }], { x: 20, y: 50 }, W, H, 4)).toBeNull();
    const v = mk("vline", [{ t: 0, p: 0 }]);
    expect(hitTest(v, [{ x: 100, y: 50 }], { x: 101, y: 280 }, W, H, 4)?.kind).toBe("body");
  });
  it("long position exposes a third (stop) handle and hits the whole box", () => {
    const d = mk(
      "long",
      [
        { t: 0, p: 100 },
        { t: 1, p: 110 },
      ],
      { stop: 95 },
    );
    const xy = [
      { x: 100, y: 100 },
      { x: 200, y: 50 },
    ];
    expect(handlePoints(d, xy, [125])).toHaveLength(3);
    expect(hitTest(d, xy, { x: 150, y: 125 }, W, H, 4, [125])).toEqual({
      kind: "handle",
      index: 2,
    });
    expect(hitTest(d, xy, { x: 120, y: 110 }, W, H, 4, [125])?.kind).toBe("body");
    expect(hitTest(d, xy, { x: 120, y: 200 }, W, H, 4, [125])).toBeNull();
  });
  it("fib hits on a level line inside its x-range and brush hits along strokes", () => {
    const f = mk("fib", [
      { t: 0, p: 100 },
      { t: 1, p: 200 },
    ]);
    const xy = [
      { x: 0, y: 200 },
      { x: 100, y: 100 },
    ]; // 0 level at y=100, 1 level at y=200
    expect(hitTest(f, xy, { x: 50, y: 150 }, W, H, 3)?.kind).toBe("body"); // 0.5
    expect(hitTest(f, xy, { x: 50, y: 130 }, W, H, 3)).toBeNull();
    const b = mk("brush", [
      { t: 0, p: 0 },
      { t: 1, p: 1 },
      { t: 2, p: 0 },
    ]);
    const bxy = [
      { x: 0, y: 0 },
      { x: 50, y: 50 },
      { x: 100, y: 0 },
    ];
    expect(hitTest(b, bxy, { x: 25, y: 26 }, W, H, 4)?.kind).toBe("body");
    expect(hitTest(b, bxy, { x: 50, y: 10 }, W, H, 4)).toBeNull();
  });
});
