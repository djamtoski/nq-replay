// Decides how to jump the chart to a journaled trade. Pure, so the branches can be tested.
import type { ReplayInfo } from "./types";

/** Seconds of chart history shown before the trade's entry. */
export const CONTEXT_S = 600;

export type ReplayPlan =
  | { kind: "seek"; ts: number } // the trade's session is the one on screen
  | { kind: "new"; request: Record<string, unknown>; seekTo: number } // recreate its session
  | { kind: "error"; message: string };

export function planReplay(info: ReplayInfo, currentSid: string): ReplayPlan {
  const ts = info.opened - CONTEXT_S;
  if (info.live && info.session_id === currentSid) return { kind: "seek", ts };
  if (!info.request) {
    return {
      kind: "error",
      message: "This trade's session was not saved, so it can't be replayed.",
    };
  }
  return { kind: "new", request: info.request, seekTo: ts };
}

export interface SessionSettings {
  source: string;
  instrument: string;
  startTime: string;
  balance: number;
  start?: string;
  end?: string;
}

/** The new-session dialog's settings, read back from a stored create-session request. */
export function settingsFromRequest(r: Record<string, unknown>): SessionSettings {
  return {
    source: String(r.source ?? "synthetic"),
    instrument: String(r.instrument ?? "NQ"),
    startTime: String(r.start_time ?? "09:00"),
    balance: Number(r.balance) || 50000,
    start: r.start ? String(r.start) : undefined,
    end: r.end ? String(r.end) : undefined,
  };
}
