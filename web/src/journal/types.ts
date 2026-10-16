// Shapes returned by the /journal/* endpoints (see journal.py).

export interface JTrade {
  id: number;
  session_id: string;
  instrument: string;
  point_value: number;
  side: "long" | "short";
  qty: number;
  entry: number;
  exit: number;
  pnl: number;
  r: number | null;
  opened: number; // epoch seconds
  closed: number;
  reason: string;
  mae: number; // points
  mfe: number;
  mae_usd: number;
  mfe_usd: number;
  day: string; // CME trading day, YYYY-MM-DD
  live: boolean;
  setup: string | null;
  emotion: string | null;
  rating: number | null;
  mistakes: string[];
  note: string;
}

export interface Notes {
  setup?: string | null;
  emotion?: string | null;
  rating?: number | null;
  mistakes?: string[];
  note?: string;
}

export interface Filters {
  start?: string;
  end?: string;
  setup?: string;
  side?: string;
}

export interface Bucket {
  key: string | number;
  trades: number;
  wins: number;
  win_rate: number;
  pnl: number;
  avg_r: number | null;
}

export interface Summary {
  trades: number;
  wins?: number;
  losses?: number;
  win_rate?: number;
  net_pnl?: number;
  gross_profit?: number;
  gross_loss?: number;
  profit_factor?: number | null;
  avg_win?: number | null;
  avg_loss?: number | null;
  expectancy?: number;
  avg_r?: number | null;
  best?: number;
  worst?: number;
  max_drawdown?: number;
  avg_hold_s?: number;
  avg_mae_usd?: number;
  avg_mfe_usd?: number;
}

export interface EquityPoint {
  t: number;
  pnl: number;
  cum: number;
}

export interface Analytics {
  summary: Summary;
  equity: EquityPoint[];
  breakdowns: Record<string, Bucket[]>;
}

export interface CalendarDay {
  day: string;
  trades: number;
  wins: number;
  pnl: number;
}

export interface CalendarMonth {
  year: number;
  month: number;
  days: CalendarDay[];
  net_pnl: number;
  trades: number;
  green_days: number;
  red_days: number;
}

export interface ReplayInfo {
  trade: number;
  session_id: string;
  live: boolean;
  request: Record<string, unknown> | null;
  opened: number;
  closed: number;
}

export interface Tags {
  setups: string[];
  emotions: string[];
  mistakes: string[];
}
