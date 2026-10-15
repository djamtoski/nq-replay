import { describe, expect, it } from "vitest";
import { CPS, MAX_REPLAY_RATE, cpsLabel, effectiveCps, feasible, replayRate } from "./speed";

describe("speed model", () => {
  it("labels read naturally", () => {
    expect(cpsLabel(0.2)).toBe("1 candle / 5s");
    expect(cpsLabel(0.5)).toBe("1 candle / 2s");
    expect(cpsLabel(1)).toBe("1 candle / s");
    expect(cpsLabel(30)).toBe("30 candles / s");
  });
  it("1 candle/s on a 1m chart is one market minute per real second", () => {
    expect(replayRate(1, 60)).toBe(60);
    expect(replayRate(1, 300)).toBe(300);
    expect(replayRate(0.2, 60)).toBeCloseTo(12);
  });
  it("feasibility follows the server cap", () => {
    expect(feasible(30, 60)).toBe(true);        // 1,800
    expect(feasible(30, 900)).toBe(true);       // 27,000
    expect(feasible(30, 3600)).toBe(false);     // 108,000
    expect(feasible(2, 14400)).toBe(true);      // 28,800
    expect(feasible(5, 14400)).toBe(false);     // 72,000
    expect(MAX_REPLAY_RATE).toBe(36_000);
  });
  it("steps down when the timeframe is too big, without losing the preference", () => {
    const pref = 30;
    expect(effectiveCps(pref, 60)).toBe(30);
    expect(effectiveCps(pref, 14400)).toBe(2);   // stepped down on 4h
    expect(effectiveCps(pref, 60)).toBe(30);     // preference intact: back on 1m it is restored
  });
  it("every timeframe offers at least one feasible speed", () => {
    for (const tf of [1, 15, 60, 300, 900, 3600, 14400]) expect(feasible(effectiveCps(CPS[0], tf), tf)).toBe(true);
    expect(effectiveCps(0.2, 14400)).toBe(0.2);
  });
});
