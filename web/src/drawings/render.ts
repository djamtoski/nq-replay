// Canvas rendering of drawings. Pure with respect to the chart: everything it needs arrives via `Proj`.
import type { Drawing } from "./types";
import {
  FIB_LEVELS,
  channelOffsetY,
  fibPrice,
  fmtDuration,
  handlePoints,
  measure,
  positionLevels,
  rayEnd,
  type XY,
} from "./geometry";

export interface Proj {
  x(t: number): number;
  y(p: number): number;
  W: number; // pane width in CSS px
  H: number; // pane height in CSS px
  pointValue: number; // USD per index point (for position tools)
  barsBetween(t0: number, t1: number): number;
  fmtPrice(p: number): string;
}

export const projectPts = (d: Drawing, P: Proj): XY[] =>
  d.pts.map((p) => ({ x: P.x(p.t), y: P.y(p.p) }));
/** Per-type derived pixel values used by hit-testing and handles (long/short: [stopY]). */
export const extraPx = (d: Drawing, P: Proj): number[] => {
  if (d.type === "long" || d.type === "short") return [P.y(positionLevels(d).stop)];
  return [];
};

const rgba = (hex: string, a: number): string => {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};

function style(ctx: CanvasRenderingContext2D, d: Drawing): void {
  ctx.strokeStyle = d.style.color;
  ctx.fillStyle = d.style.color;
  ctx.lineWidth = d.style.width;
  ctx.setLineDash(d.style.dash ? [6, 4] : []);
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
}

const seg = (ctx: CanvasRenderingContext2D, a: XY, b: XY) => {
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.stroke();
};

function label(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  fg: string,
  bg: string,
  align: CanvasTextAlign = "left",
): void {
  ctx.save();
  ctx.setLineDash([]);
  ctx.font = "12px -apple-system, Segoe UI, sans-serif";
  const w = ctx.measureText(text).width + 8;
  const left = align === "right" ? x - w : align === "center" ? x - w / 2 : x;
  ctx.fillStyle = bg;
  ctx.fillRect(left, y - 9, w, 18);
  ctx.fillStyle = fg;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillText(text, left + 4, y);
  ctx.restore();
}

export function drawDrawing(
  ctx: CanvasRenderingContext2D,
  d: Drawing,
  P: Proj,
  selected: boolean,
): void {
  const xy = projectPts(d, P);
  if (xy.length === 0 || xy.some((p) => !Number.isFinite(p.x) || !Number.isFinite(p.y))) return;
  const [a, b, c] = xy;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, P.W, P.H);
  ctx.clip();
  style(ctx, d);

  switch (d.type) {
    case "trend":
      if (b) seg(ctx, a, b);
      break;
    case "ray":
      if (b) seg(ctx, a, rayEnd(a, b, P.W, P.H));
      break;
    case "arrow":
      if (b) {
        seg(ctx, a, b);
        arrowHead(ctx, a, b, 6 + d.style.width * 2);
      }
      break;
    case "hline":
      seg(ctx, { x: 0, y: a.y }, { x: P.W, y: a.y });
      label(ctx, P.fmtPrice(d.pts[0].p), P.W - 2, a.y, "#fff", d.style.color, "right");
      break;
    case "hray":
      seg(ctx, a, { x: P.W, y: a.y });
      label(ctx, P.fmtPrice(d.pts[0].p), P.W - 2, a.y, "#fff", d.style.color, "right");
      break;
    case "vline":
      seg(ctx, { x: a.x, y: 0 }, { x: a.x, y: P.H });
      break;
    case "rect":
      if (b) {
        ctx.fillStyle = rgba(d.style.color, 0.15);
        ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
        ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
      }
      break;
    case "ellipse":
      if (b) {
        ctx.beginPath();
        ctx.ellipse(
          (a.x + b.x) / 2,
          (a.y + b.y) / 2,
          Math.abs(b.x - a.x) / 2,
          Math.abs(b.y - a.y) / 2,
          0,
          0,
          Math.PI * 2,
        );
        ctx.fillStyle = rgba(d.style.color, 0.12);
        ctx.fill();
        ctx.stroke();
      }
      break;
    case "channel":
      if (b) {
        const off = c ? channelOffsetY(a, b, c) : 0;
        const a2 = { x: a.x, y: a.y + off },
          b2 = { x: b.x, y: b.y + off };
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.lineTo(b2.x, b2.y);
        ctx.lineTo(a2.x, a2.y);
        ctx.closePath();
        ctx.fillStyle = rgba(d.style.color, 0.1);
        ctx.fill();
        seg(ctx, a, b);
        seg(ctx, a2, b2);
        ctx.setLineDash([4, 4]);
        seg(ctx, { x: a.x, y: a.y + off / 2 }, { x: b.x, y: b.y + off / 2 });
      }
      break;
    case "fib":
      if (b) drawFib(ctx, d, a, b, P);
      break;
    case "long":
    case "short":
      if (b) drawPosition(ctx, d, a, b, P);
      break;
    case "measure":
      if (b) drawMeasure(ctx, d, a, b, P);
      break;
    case "text":
      ctx.font = `${12 + d.style.width * 2}px -apple-system, Segoe UI, sans-serif`;
      ctx.textBaseline = "middle";
      ctx.fillText(d.text ?? "", a.x, a.y);
      break;
    case "brush":
      if (xy.length > 1) {
        ctx.beginPath();
        ctx.moveTo(xy[0].x, xy[0].y);
        for (let i = 1; i < xy.length; i++) ctx.lineTo(xy[i].x, xy[i].y);
        ctx.stroke();
      }
      break;
  }

  if (selected) {
    ctx.setLineDash([]);
    for (const h of handlePoints(d, xy, extraPx(d, P))) {
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = d.style.color;
      ctx.lineWidth = 1.5;
      ctx.fillRect(h.x - 4, h.y - 4, 8, 8);
      ctx.strokeRect(h.x - 4, h.y - 4, 8, 8);
    }
  }
  ctx.restore();
}

function arrowHead(ctx: CanvasRenderingContext2D, a: XY, b: XY, size: number): void {
  const ang = Math.atan2(b.y - a.y, b.x - a.x);
  ctx.beginPath();
  ctx.moveTo(b.x, b.y);
  ctx.lineTo(b.x - size * Math.cos(ang - Math.PI / 7), b.y - size * Math.sin(ang - Math.PI / 7));
  ctx.lineTo(b.x - size * Math.cos(ang + Math.PI / 7), b.y - size * Math.sin(ang + Math.PI / 7));
  ctx.closePath();
  ctx.fill();
}

function drawFib(ctx: CanvasRenderingContext2D, d: Drawing, a: XY, b: XY, P: Proj): void {
  const x0 = Math.min(a.x, b.x),
    x1 = Math.max(a.x, b.x);
  const ys = FIB_LEVELS.map((l) => P.y(fibPrice(d.pts[0].p, d.pts[1].p, l)));
  for (let i = 1; i < ys.length; i++) {
    ctx.fillStyle = rgba(d.style.color, i % 2 ? 0.07 : 0.14);
    ctx.fillRect(x0, Math.min(ys[i - 1], ys[i]), x1 - x0, Math.abs(ys[i] - ys[i - 1]));
  }
  FIB_LEVELS.forEach((l, i) => {
    ctx.strokeStyle = d.style.color;
    ctx.lineWidth = i === 0 || i === FIB_LEVELS.length - 1 ? d.style.width : 1;
    seg(ctx, { x: x0, y: ys[i] }, { x: x1, y: ys[i] });
    label(
      ctx,
      `${l} (${P.fmtPrice(fibPrice(d.pts[0].p, d.pts[1].p, l))})`,
      x0 + 2,
      ys[i] - 11,
      "#fff",
      rgba(d.style.color, 0.85),
    );
  });
  ctx.setLineDash([3, 4]);
  ctx.lineWidth = 1;
  seg(ctx, a, b);
}

function drawPosition(ctx: CanvasRenderingContext2D, d: Drawing, a: XY, b: XY, P: Proj): void {
  const lv = positionLevels(d);
  const x0 = Math.min(a.x, b.x),
    w = Math.max(24, Math.abs(b.x - a.x));
  const yE = P.y(lv.entry),
    yT = P.y(lv.target),
    yS = P.y(lv.stop);
  const green = "#26a69a",
    red = "#ef5350";
  ctx.fillStyle = rgba(green, 0.22);
  ctx.fillRect(x0, Math.min(yE, yT), w, Math.abs(yT - yE));
  ctx.fillStyle = rgba(red, 0.22);
  ctx.fillRect(x0, Math.min(yE, yS), w, Math.abs(yS - yE));
  ctx.setLineDash([]);
  ctx.lineWidth = 1;
  ctx.strokeStyle = green;
  seg(ctx, { x: x0, y: yT }, { x: x0 + w, y: yT });
  ctx.strokeStyle = red;
  seg(ctx, { x: x0, y: yS }, { x: x0 + w, y: yS });
  ctx.strokeStyle = d.style.color;
  ctx.lineWidth = 1.5;
  seg(ctx, { x: x0, y: yE }, { x: x0 + w, y: yE });
  const usd = (pts: number) =>
    `$${(pts * P.pointValue).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
  const mid = x0 + w / 2;
  label(
    ctx,
    `Target ${P.fmtPrice(lv.target)}  (${lv.rewardPts.toFixed(2)} pts, ${usd(lv.rewardPts)})`,
    mid,
    (yT + yE) / 2,
    "#fff",
    rgba(green, 0.9),
    "center",
  );
  label(
    ctx,
    `Stop ${P.fmtPrice(lv.stop)}  (${lv.riskPts.toFixed(2)} pts, ${usd(lv.riskPts)})`,
    mid,
    (yS + yE) / 2,
    "#fff",
    rgba(red, 0.9),
    "center",
  );
  label(
    ctx,
    `${d.type === "long" ? "Long" : "Short"}  R:R ${lv.rr.toFixed(2)}`,
    mid,
    yE,
    "#fff",
    rgba("#1e222d", 0.9),
    "center",
  );
}

function drawMeasure(ctx: CanvasRenderingContext2D, d: Drawing, a: XY, b: XY, P: Proj): void {
  const m = measure(d.pts[0], d.pts[1], P.barsBetween(d.pts[0].t, d.pts[1].t));
  const up = m.dPrice >= 0;
  const col = up ? "#2962ff" : "#ef5350";
  ctx.fillStyle = rgba(col, 0.15);
  ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
  ctx.strokeStyle = col;
  ctx.fillStyle = col;
  ctx.setLineDash([]);
  ctx.lineWidth = 1.5;
  seg(ctx, a, b);
  arrowHead(ctx, a, b, 8);
  const txt = `${m.dPrice >= 0 ? "+" : ""}${m.dPrice.toFixed(2)} (${m.pct >= 0 ? "+" : ""}${m.pct.toFixed(2)}%) · ${Math.round(m.bars)} bars · ${fmtDuration(m.seconds)}`;
  label(
    ctx,
    txt,
    (a.x + b.x) / 2,
    up ? Math.min(a.y, b.y) - 14 : Math.max(a.y, b.y) + 14,
    "#fff",
    col,
    "center",
  );
}
