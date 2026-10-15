// Protocol types + pure state reducer (no DOM) so it can be unit-tested.

export interface Bar { t: number; o: number; h: number; l: number; c: number; v: number }
export interface Position { id: number; side: "long" | "short"; qty: number; entry: number; sl: number | null; tp: number | null; trail_ticks: number | null; opened: number }
export interface OrderRow { id: number; side: "long" | "short"; qty: number; otype: string; price: number | null; sl: number | null; tp: number | null; status: string }
export interface Trade { side: "long" | "short"; qty: number; entry: number; exit: number; pnl: number; r: number | null; opened: number; closed: number; reason: string }
export interface Stats { trades: number; win_rate?: number; net_pnl?: number; profit_factor?: number | null; avg_r?: number | null; max_drawdown_usd?: number }
export interface Account { balance: number; equity: number; margin: number; unrealized: number; stats: Stats }
export interface SessionRanges { asia: { hi: number | null; lo: number | null } }

export interface State {
  cursor: number; total: number; now: number;
  playback: "playing" | "paused"; speed: number;
  bars: Record<string, Bar[]>;
  forming: Record<string, Bar | null>;
  positions: Position[]; orders: OrderRow[]; trades: Trade[];
  account: Account | null; sessions: SessionRanges | null;
  lastError: string | null; ended: boolean;
}

export type ServerMsg =
  | { type: "reset"; tf: number; now: number; cursor: number; total: number; state: string; speed: number;
      history: Record<string, Bar[]>; forming: Record<string, Bar | null>;
      positions: Position[]; orders: OrderRow[]; trades: Trade[]; account: Account; sessions: SessionRanges }
  | { type: "view"; tf: number; history: Record<string, Bar[]>; forming: Record<string, Bar | null> }
  | { type: "tick"; tf: number; now: number; cursor: number; total: number; bar: Bar | null;
      closed: Record<string, Bar[]>; forming: Record<string, Bar | null>;
      positions: Position[]; orders: OrderRow[]; new_trades: Trade[]; account: Account; sessions: SessionRanges }
  | { type: "state"; state: string; speed: number }
  | { type: "end"; account: Account }
  | { type: "order_ack"; order_id: number; status: string }
  | { type: "error"; error: string };

export function initial(): State {
  return { cursor: 0, total: 0, now: 0, playback: "paused", speed: 1, bars: {}, forming: {}, positions: [],
    orders: [], trades: [], account: null, sessions: null, lastError: null, ended: false };
}

export const MAX_BARS = 6000;

/**
 * Bar data is per timeframe. A message built for a timeframe the user has already left (it was in flight when they
 * switched) must not repaint the chart; the `view` reply for the current timeframe does that. Account, positions
 * and trades from such a message are still valid and are applied by apply() regardless.
 */
export const drawsChart = (m: ServerMsg, tf: number): boolean =>
  (m.type === "reset" || m.type === "view" || m.type === "tick") && m.tf === tf;

/** Replay time (epoch s) after stepping forward to the end of the current/next `tf`-second candle. */
export const nextCandleEnd = (now: number, tf: number) => (Math.floor(now / tf) + 1) * tf;
/** Replay time to rewind to so the previous candle boundary is the last revealed instant. */
export const prevCandleEnd = (now: number, tf: number) => (now % tf === 0 ? now - tf : Math.floor(now / tf) * tf);

const pb = (s: string): State["playback"] => (s === "playing" ? "playing" : "paused");

/** Applies one server message. Mutates and returns `s` (cheap at 100x playback). */
export function apply(s: State, m: ServerMsg): State {
  switch (m.type) {
    case "reset":
      s.cursor = m.cursor; s.total = m.total; s.now = m.now; s.playback = pb(m.state); s.speed = m.speed;
      s.bars = m.history; s.forming = m.forming; s.positions = m.positions; s.orders = m.orders;
      s.trades = m.trades; s.account = m.account; s.sessions = m.sessions; s.ended = false;
      break;
    case "view":    // the server switched this client to another timeframe: replace, never merge, its bars
      s.bars = m.history; s.forming = m.forming;
      break;
    case "tick":
      s.cursor = m.cursor; s.total = m.total; s.now = m.now;
      for (const [tf, nb] of Object.entries(m.closed)) {
        const arr = (s.bars[tf] ??= []);
        for (const b of nb) if (!arr.length || b.t > arr[arr.length - 1].t) arr.push(b);
        if (arr.length > MAX_BARS) arr.splice(0, arr.length - MAX_BARS);   // keep memory bounded on long plays
      }
      Object.assign(s.forming, m.forming);      // the server sends only the viewed timeframe's forming bar
      s.positions = m.positions; s.orders = m.orders;
      s.trades.push(...m.new_trades); s.account = m.account; s.sessions = m.sessions;
      break;
    case "state": s.playback = pb(m.state); s.speed = m.speed; break;
    case "end": s.account = m.account; s.ended = true; s.playback = "paused"; break;
    case "error": s.lastError = m.error; break;
    case "order_ack": s.lastError = m.status === "rejected" ? "order rejected (margin or invalid price)" : null; break;
  }
  return s;
}

export const fmt = (x: number | null | undefined, d = 2) =>
  x === null || x === undefined || !Number.isFinite(x) ? "–" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

/** SL/TP given as distance in points from the entry price, signed by side. */
export function bracket(side: "long" | "short", entry: number, slPts: number | null, tpPts: number | null) {
  const s = side === "long" ? 1 : -1;
  return { sl: slPts ? entry - s * slPts : null, tp: tpPts ? entry + s * tpPts : null };
}
