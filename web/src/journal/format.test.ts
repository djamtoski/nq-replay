import { describe, expect, it } from "vitest";
import {
  bucketLabel,
  calendarGrid,
  duration,
  equityPath,
  hourLabel,
  intensity,
  money,
  num,
  pct,
  pnlClass,
  shiftMonth,
  signedMoney,
} from "./format";

describe("number formatting", () => {
  it("formats money with sign and thousands separators", () => {
    expect(money(1234.5, 2)).toBe("$1,234.50");
    expect(money(-50)).toBe("-$50");
    expect(money(0)).toBe("$0");
    expect(money(null)).toBe("–");
    expect(money(Number.NaN)).toBe("–");
    expect(money(Infinity)).toBe("–");
  });
  it("puts a plus on positive signed money only", () => {
    expect(signedMoney(10)).toBe("+$10");
    expect(signedMoney(-10)).toBe("-$10");
    expect(signedMoney(0)).toBe("$0");
    expect(signedMoney(undefined)).toBe("–");
  });
  it("formats percentages and plain numbers, with a dash for missing values", () => {
    expect(pct(0.6)).toBe("60%");
    expect(pct(0.1234, 1)).toBe("12.3%");
    expect(pct(null)).toBe("–");
    expect(num(1.005, 1)).toBe("1.0");
    expect(num(null)).toBe("–");
    expect(num(Number.NaN)).toBe("–");
  });
  it("classes by sign", () => {
    expect(pnlClass(5)).toBe("pos");
    expect(pnlClass(-5)).toBe("neg");
    expect(pnlClass(0)).toBe("");
    expect(pnlClass(null)).toBe("");
  });
});

describe("duration", () => {
  it.each([
    [0, "0s"],
    [59, "59s"],
    [60, "1m 0s"],
    [75, "1m 15s"],
    [3599, "59m 59s"],
    [3600, "1h 0m"],
    [3700, "1h 1m"],
    [-1, "–"],
    [Number.NaN, "–"],
  ])("%s -> %s", (s, out) => expect(duration(s)).toBe(out));
});

describe("labels", () => {
  it("labels hours and weekdays, falls back to the raw key", () => {
    expect(hourLabel(9)).toBe("09:00");
    expect(bucketLabel("hour", 14)).toBe("14:00");
    expect(bucketLabel("weekday", 0)).toBe("Mon");
    expect(bucketLabel("weekday", 6)).toBe("Sun");
    expect(bucketLabel("weekday", 9)).toBe("9");
    expect(bucketLabel("setup", "ORB")).toBe("ORB");
  });
});

describe("calendarGrid", () => {
  it("starts weeks on Monday and pads with nulls", () => {
    const g = calendarGrid(2025, 3, []); // 1 Mar 2025 is a Saturday
    expect(g[0].slice(0, 5).every((c) => c === null)).toBe(true);
    expect(g[0][5]?.day).toBe("2025-03-01");
    expect(g[0][6]?.day).toBe("2025-03-02");
    expect(g.every((w) => w.length === 7)).toBe(true);
    expect(g.flat().filter(Boolean)).toHaveLength(31);
    const last = g[g.length - 1].filter(Boolean).pop();
    expect(last?.day).toBe("2025-03-31");
  });
  it("handles a month that starts on Monday and February in a leap year", () => {
    expect(calendarGrid(2025, 9, [])[0][0]?.day).toBe("2025-09-01");
    expect(calendarGrid(2024, 2, []).flat().filter(Boolean)).toHaveLength(29);
    expect(calendarGrid(2025, 2, []).flat().filter(Boolean)).toHaveLength(28);
  });
  it("attaches trade totals to the right day", () => {
    const g = calendarGrid(2025, 3, [{ day: "2025-03-11", trades: 3, wins: 2, pnl: 300 }]);
    const cell = g.flat().find((c) => c?.day === "2025-03-11");
    expect(cell).toMatchObject({ n: 11, trades: 3, wins: 2, pnl: 300 });
    expect(g.flat().find((c) => c?.day === "2025-03-12")).toMatchObject({ trades: 0, pnl: 0 });
  });
});

describe("shiftMonth", () => {
  it("rolls over year boundaries in both directions", () => {
    expect(shiftMonth(2025, 12, 1)).toEqual([2026, 1]);
    expect(shiftMonth(2025, 1, -1)).toEqual([2024, 12]);
    expect(shiftMonth(2025, 3, 0)).toEqual([2025, 3]);
    expect(shiftMonth(2025, 3, 14)).toEqual([2026, 5]);
    expect(shiftMonth(2025, 3, -15)).toEqual([2023, 12]);
  });
});

describe("intensity", () => {
  it("scales between 0.15 and 0.9 and is 0 without a pnl or a scale", () => {
    expect(intensity(0, 100)).toBe(0);
    expect(intensity(50, 0)).toBe(0);
    expect(intensity(100, 100)).toBeCloseTo(0.9);
    expect(intensity(-100, 100)).toBeCloseTo(0.9);
    expect(intensity(1, 100)).toBeGreaterThan(0.15);
    expect(intensity(500, 100)).toBeCloseTo(0.9); // clamped
  });
});

describe("equityPath", () => {
  const pts = [
    { t: 1, pnl: 100, cum: 100 },
    { t: 2, pnl: -150, cum: -50 },
    { t: 3, pnl: 250, cum: 200 },
  ];
  it("starts at zero, spans the box and keeps the zero line inside it", () => {
    const c = equityPath(pts, 100, 50, 5);
    expect(c.min).toBe(-50);
    expect(c.max).toBe(200);
    expect(c.d.startsWith("M5.0 ")).toBe(true);
    expect(c.d.match(/L/g)).toHaveLength(3);
    expect(c.d.endsWith("95.0 5.0")).toBe(true); // the maximum is drawn at the top padding
    expect(c.zeroY).toBeGreaterThan(5);
    expect(c.zeroY).toBeLessThan(45);
  });
  it("does not divide by zero for a flat or empty curve", () => {
    expect(equityPath([], 100, 50).d).toMatch(/^M[\d.]+ [\d.]+$/);
    const flat = equityPath([{ t: 1, pnl: 0, cum: 0 }], 100, 50);
    expect(flat.d).not.toMatch(/NaN|Infinity/);
  });
});
