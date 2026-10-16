import { describe, expect, it } from "vitest";
import { CONTEXT_S, planReplay, settingsFromRequest } from "./replay";
import type { ReplayInfo } from "./types";

const info = (over: Partial<ReplayInfo> = {}): ReplayInfo => ({
  trade: 1,
  session_id: "abc",
  live: true,
  request: { source: "synthetic" },
  opened: 10_000,
  closed: 10_100,
  ...over,
});

describe("planReplay", () => {
  it("seeks inside the session that is already on screen", () => {
    expect(planReplay(info(), "abc")).toEqual({ kind: "seek", ts: 10_000 - CONTEXT_S });
  });
  it("recreates the session when the trade is from another one", () => {
    const p = planReplay(info({ session_id: "old", live: false }), "abc");
    expect(p).toEqual({
      kind: "new",
      request: { source: "synthetic" },
      seekTo: 10_000 - CONTEXT_S,
    });
  });
  it("recreates it when the session still exists on the server but is not the one on screen", () => {
    expect(planReplay(info({ session_id: "other", live: true }), "abc").kind).toBe("new");
  });
  it("does not trust a stale live flag for a different session id", () => {
    expect(planReplay(info({ live: false }), "abc").kind).toBe("new"); // same id but ended: recreate
  });
  it("explains when the stored session is unknown", () => {
    const p = planReplay(info({ session_id: "old", live: false, request: null }), "abc");
    expect(p.kind).toBe("error");
    expect(p.kind === "error" && p.message).toContain("not saved");
  });
  it("still seeks a live on-screen session even without a stored request", () => {
    expect(planReplay(info({ request: null }), "abc").kind).toBe("seek");
  });
});

describe("settingsFromRequest", () => {
  it("reads the dialog fields back", () => {
    expect(
      settingsFromRequest({
        source: "parquet",
        instrument: "MNQ",
        start_time: "08:30",
        balance: 25000,
        start: "2025-03-03",
        end: "2025-03-07",
      }),
    ).toEqual({
      source: "parquet",
      instrument: "MNQ",
      startTime: "08:30",
      balance: 25000,
      start: "2025-03-03",
      end: "2025-03-07",
    });
  });
  it("falls back to defaults for a sparse request", () => {
    expect(settingsFromRequest({})).toEqual({
      source: "synthetic",
      instrument: "NQ",
      startTime: "09:00",
      balance: 50000,
      start: undefined,
      end: undefined,
    });
    expect(settingsFromRequest({ balance: "junk", start: null }).balance).toBe(50000);
  });
});
