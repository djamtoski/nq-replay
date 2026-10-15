// Playback speed model. Speed is "candles of the CHART timeframe per real second": on a 1m chart,
// 1 candle/s means one market minute passes each real second. Pure functions so they can be tested.

export const CPS = [0.2, 0.5, 1, 2, 5, 10, 30];

/** Replay-seconds per real second the server can step (2000 bars x 20 frames/s = 40k, with margin). */
export const MAX_REPLAY_RATE = 36_000;

export const cpsLabel = (v: number): string =>
  v < 1 ? `1 candle / ${Math.round(1 / v)}s` : v === 1 ? "1 candle / s" : `${v} candles / s`;

export const feasible = (cps: number, tf: number): boolean => cps * tf <= MAX_REPLAY_RATE;

/**
 * The speed actually used: the user's preference, stepped down to the fastest feasible option when the
 * timeframe is too large for it. The preference itself is never overwritten, so returning to a small
 * timeframe restores the speed the user chose.
 */
export function effectiveCps(preferred: number, tf: number): number {
  if (feasible(preferred, tf)) return preferred;
  return [...CPS].reverse().find((v) => feasible(v, tf)) ?? CPS[0];
}

/** Replay-seconds per real second to send to the server. */
export const replayRate = (cps: number, tf: number): number => cps * tf;
