// Pure helpers for the journal views (no DOM): formatting, calendar grid, equity-curve path.
import type { CalendarDay, EquityPoint } from "./types";

export const money = (v: number | null | undefined, d = 0): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  const s = Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
  return `${v < 0 ? "-" : ""}$${s}`;
};

export const signedMoney = (v: number | null | undefined, d = 0): string =>
  v !== null && v !== undefined && Number.isFinite(v) && v > 0 ? `+${money(v, d)}` : money(v, d);

export const pct = (v: number | null | undefined, d = 0): string =>
  v === null || v === undefined || !Number.isFinite(v) ? "–" : `${(v * 100).toFixed(d)}%`;

export const num = (v: number | null | undefined, d = 2): string =>
  v === null || v === undefined || !Number.isFinite(v) ? "–" : v.toFixed(d);

export const pnlClass = (v: number | null | undefined): string => (!v ? "" : v > 0 ? "pos" : "neg");

/** 75 -> "1m 15s", 3700 -> "1h 1m". */
export function duration(s: number): string {
  if (!Number.isFinite(s) || s < 0) return "–";
  const t = Math.round(s);
  if (t < 60) return `${t}s`;
  if (t < 3600) return `${Math.floor(t / 60)}m ${t % 60}s`;
  return `${Math.floor(t / 3600)}h ${Math.floor((t % 3600) / 60)}m`;
}

export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

export const hourLabel = (h: number): string => `${String(h).padStart(2, "0")}:00`;

/** Label for a breakdown bucket key. */
export function bucketLabel(kind: string, key: string | number): string {
  if (kind === "hour") return hourLabel(Number(key));
  if (kind === "weekday") return WEEKDAYS[Number(key)] ?? String(key);
  return String(key);
}

export interface Cell {
  day: string; // YYYY-MM-DD
  n: number; // day of month
  pnl: number;
  trades: number;
  wins: number;
}

const iso = (y: number, m: number, d: number) =>
  `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;

/**
 * Month grid, Monday first. Cells outside the month are null. `month` is 1-12.
 * Uses UTC date arithmetic so the result never depends on the machine's time zone.
 */
export function calendarGrid(year: number, month: number, days: CalendarDay[]): (Cell | null)[][] {
  const byDay = new Map(days.map((d) => [d.day, d]));
  const count = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const lead = (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7; // Mon=0
  const cells: (Cell | null)[] = Array(lead).fill(null);
  for (let d = 1; d <= count; d++) {
    const key = iso(year, month, d);
    const x = byDay.get(key);
    cells.push({ day: key, n: d, pnl: x?.pnl ?? 0, trades: x?.trades ?? 0, wins: x?.wins ?? 0 });
  }
  while (cells.length % 7) cells.push(null);
  const weeks: (Cell | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

export function shiftMonth(year: number, month: number, delta: number): [number, number] {
  const i = year * 12 + (month - 1) + delta;
  return [Math.floor(i / 12), (i % 12) + 1];
}

/** Opacity (0.15-0.9) for a calendar cell's colour, scaled to the month's largest absolute day. */
export function intensity(pnl: number, maxAbs: number): number {
  if (!pnl || !maxAbs) return 0;
  return 0.15 + 0.75 * Math.min(1, Math.abs(pnl) / maxAbs);
}

export interface Curve {
  d: string;
  zeroY: number;
  min: number;
  max: number;
}

/** SVG path for the cumulative P&L curve inside a w x h box (with `pad` px margin). */
export function equityPath(points: EquityPoint[], w: number, h: number, pad = 6): Curve {
  const vals = [0, ...points.map((p) => p.cum)];
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const y = (v: number) => pad + (1 - (v - min) / span) * (h - 2 * pad);
  const n = vals.length;
  const x = (i: number) => pad + (n === 1 ? 0 : (i / (n - 1)) * (w - 2 * pad));
  const d = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  return { d, zeroY: y(0), min, max };
}
