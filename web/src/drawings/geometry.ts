// Pure geometry for drawings (no DOM / chart dependency) so it can be unit-tested.
import type { Drawing, Pt } from "./types";

export interface XY { x: number; y: number }

// ---------------------------------------------------------------- time <-> logical index
// The chart is index-based (bar 0, 1, 2 ...). Drawings are stored in time. These map between the two,
// interpolating between bars and extrapolating by `interval` seconds before the first / after the last
// bar. Extrapolation is what lets a drawing sit in the empty space to the right of "now" without any
// future data existing (replay has no look-ahead).
export function timeToLogical(times: number[], t: number, interval: number): number {
  const n = times.length;
  if (n === 0) return 0;
  if (n === 1 || t <= times[0]) return (t - times[0]) / interval;
  if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / interval;
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (times[mid] <= t) lo = mid; else hi = mid; }
  return lo + (t - times[lo]) / (times[lo + 1] - times[lo]);
}

export function logicalToTime(times: number[], l: number, interval: number): number {
  const n = times.length;
  if (n === 0) return 0;
  if (l <= 0) return times[0] + l * interval;
  if (l >= n - 1) return times[n - 1] + (l - (n - 1)) * interval;
  const i = Math.floor(l);
  return times[i] + (l - i) * (times[i + 1] - times[i]);
}

// ---------------------------------------------------------------- 2D helpers
export function distToSegment(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x, dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const u = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
  return Math.hypot(p.x - (a.x + u * dx), p.y - (a.y + u * dy));
}

/** Point where the ray a->b leaves the pane [0,w]x[0,h]. */
export function rayEnd(a: XY, b: XY, w: number, h: number): XY {
  const dx = b.x - a.x, dy = b.y - a.y;
  if (dx === 0 && dy === 0) return b;
  let t = Infinity;
  if (dx > 0) t = Math.min(t, (w - a.x) / dx); else if (dx < 0) t = Math.min(t, (0 - a.x) / dx);
  if (dy > 0) t = Math.min(t, (h - a.y) / dy); else if (dy < 0) t = Math.min(t, (0 - a.y) / dy);
  return { x: a.x + dx * t, y: a.y + dy * t };
}

export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
/** Level 0 sits at the END point, level 1 at the START point (TradingView convention). */
export const fibPrice = (p0: number, p1: number, lvl: number) => p1 + (p0 - p1) * lvl;

/** Parallel channel: the second line passes through pts[2] with the slope of pts[0]->pts[1]. */
export function channelOffsetY(a: XY, b: XY, c: XY): number {
  const dx = b.x - a.x;
  const lineYatC = dx === 0 ? a.y : a.y + ((c.x - a.x) * (b.y - a.y)) / dx;
  return c.y - lineYatC;
}

export interface PositionLevels { entry: number; target: number; stop: number; rewardPts: number; riskPts: number; rr: number }
export function positionLevels(d: Drawing): PositionLevels {
  const entry = d.pts[0].p, target = d.pts[1].p;
  const stop = d.stop ?? entry - (target - entry);
  const reward = Math.abs(target - entry), risk = Math.abs(entry - stop);
  return { entry, target, stop, rewardPts: reward, riskPts: risk, rr: risk === 0 ? 0 : reward / risk };
}

export interface MeasureInfo { dPrice: number; pct: number; bars: number; seconds: number }
export function measure(a: Pt, b: Pt, barsBetween: number): MeasureInfo {
  return { dPrice: b.p - a.p, pct: a.p === 0 ? 0 : ((b.p - a.p) / a.p) * 100, bars: barsBetween, seconds: b.t - a.t };
}

export function fmtDuration(sec: number): string {
  const s = Math.abs(Math.round(sec));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const parts = [d && `${d}d`, h && `${h}h`, m && `${m}m`, !d && !h && r && `${r}s`].filter(Boolean);
  return parts.length ? parts.join(" ") : "0s";
}

// ---------------------------------------------------------------- hit testing (pixel space)
export interface Hit { kind: "body" | "handle"; index: number }

export function inBox(p: XY, a: XY, b: XY, pad = 0): boolean {
  return p.x >= Math.min(a.x, b.x) - pad && p.x <= Math.max(a.x, b.x) + pad &&
         p.y >= Math.min(a.y, b.y) - pad && p.y <= Math.max(a.y, b.y) + pad;
}

/**
 * `xy` are the drawing's points projected to pixels. `extra` carries per-type derived pixels:
 *   long/short -> [stopY], channel -> [offsetY]. Returns null when the pointer misses.
 */
export function hitTest(d: Drawing, xy: XY[], p: XY, w: number, h: number, tol: number, extra: number[] = []): Hit | null {
  // handles first so they win over the body
  const handles = handlePoints(d, xy, extra);
  for (let i = 0; i < handles.length; i++) if (Math.hypot(p.x - handles[i].x, p.y - handles[i].y) <= tol + 2) return { kind: "handle", index: i };
  const body = (ok: boolean): Hit | null => (ok ? { kind: "body", index: -1 } : null);
  const [a, b, c] = xy;
  switch (d.type) {
    case "trend": case "arrow": case "measure": return body(distToSegment(p, a, b) <= tol || (d.type === "measure" && inBox(p, a, b)));
    case "ray": return body(distToSegment(p, a, rayEnd(a, b, w, h)) <= tol);
    case "hline": return body(Math.abs(p.y - a.y) <= tol);
    case "hray": return body(Math.abs(p.y - a.y) <= tol && p.x >= a.x - tol);
    case "vline": return body(Math.abs(p.x - a.x) <= tol);
    case "rect": case "ellipse": return body(inBox(p, a, b, tol));
    case "channel": {
      const off = channelOffsetY(a, b, c ?? b);
      return body(distToSegment(p, a, b) <= tol || distToSegment(p, { x: a.x, y: a.y + off }, { x: b.x, y: b.y + off }) <= tol);
    }
    case "fib": {
      const x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x);
      return body(p.x >= x0 - tol && p.x <= x1 + tol && FIB_LEVELS.some((l) => Math.abs(p.y - (b.y + (a.y - b.y) * l)) <= tol));
    }
    case "long": case "short": {
      const stopY = extra[0] ?? a.y;
      return body(inBox(p, { x: a.x, y: Math.min(a.y, b.y, stopY) }, { x: b.x, y: Math.max(a.y, b.y, stopY) }, 0));
    }
    case "text": return body(p.x >= a.x - 2 && p.x <= a.x + Math.max(30, (d.text?.length ?? 4) * 8) && p.y >= a.y - 12 && p.y <= a.y + 6);
    case "brush": for (let i = 1; i < xy.length; i++) if (distToSegment(p, xy[i - 1], xy[i]) <= tol) return body(true); return null;
  }
}

/** Draggable handle positions in pixels (index matches the point being edited; long/short stop is index 2). */
export function handlePoints(d: Drawing, xy: XY[], extra: number[] = []): XY[] {
  if (d.type === "brush") return [];
  if (d.type === "long" || d.type === "short") return [xy[0], xy[1], { x: (xy[0].x + xy[1].x) / 2, y: extra[0] ?? xy[0].y }];
  if (d.type === "channel") {
    const off = channelOffsetY(xy[0], xy[1], xy[2] ?? xy[1]);
    return [xy[0], xy[1], { x: xy[2]?.x ?? xy[1].x, y: (xy[2]?.y) ?? xy[1].y + off }];
  }
  return xy.slice();
}
