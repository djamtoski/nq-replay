# Replay/backtest engine for NQ (US100) – engineering breakdown

**Provenance.** FXReplay's code was *not* analyzed. Its public FAQ confirms only: futures/stocks/indices/crypto/forex symbols; a playback bar with manual stepping, adjustable speed and "Replay to This Date"; market/limit/stop orders; a Position Drawer with partial close; a Spreads & Commissions account tab; sessions that store trades/stats; PnL, win rate, RR, calendar and Monte Carlo analytics. It states **nothing** about data providers, timeframes, tick data, slippage, margin or SL/TP/trailing mechanics. Everything below is standard replay-engine design, not a claim about how FXReplay is built. Candidate providers are options, not findings.

## 1. Data ingestion and pipeline

| Concern | Recommendation for NQ |
|---|---|
| Source | Databento `GLBX.MDP3` (CME) – `ohlcv-1s` / `trades` / `mbp-1`; alternatives: Polygon futures, Rithmic/CQG historical, Kibot. Equities proxy QQQ: Polygon/Databento XNAS. |
| Base resolution | Store **1s** bars (fits 1s/1m replay). 1m+ are derived, never stored separately (single source of truth). Tick/`trades` only if you need intrabar fill truth. |
| Storage | Parquet, one file per symbol/day: `bars/NQ/1s/YYYY/YYYY-MM-DD.parquet`; cols `ts(int64 ns open), o,h,l,c, v, instrument_id`. Zstd, sorted by ts, row groups ≈ 1h so seek is cheap. ~86k rows/day max; ~6 yrs ≈ 2–3 GB. |
| Contracts | Keep raw per-contract bars + roll table (date, from, to, gap). Apply back-adjustment at read time; roll on volume crossover, not calendar. |
| Serving | Backend returns a window `[t0, t1)` of base bars (chunked, e.g. 1 trading day) as Arrow/Parquet or binary; client prefetches the next chunk while playing. Redis/LRU for hot days; CDN for immutable day files. |
| Aggregation | Done client- or engine-side from base bars (see §2), so every timeframe is consistent by construction. |
| Gaps | CME maintenance 17:00–18:00 NY and holidays: no synthetic bars; clock jumps to next bar. |

## 2. Replay engine and time machine

- **Cursor model.** State = index of the next unrevealed base bar. Strategy/UI code can only read `bars[:cursor]` → look-ahead is structurally impossible rather than policed.
- **Controls.** `step(n)`, `play` (async loop, sleep = bar duration ÷ speed; 1×–100× is just the divisor; above ~10× batch several bars per frame), `pause`, `seek(ts)` (binary search + fast-forward), `step_back` (restore nearest periodic snapshot of broker/aggregator state, then re-step – cheaper and safer than un-applying fills).
- **Multi-timeframe without look-ahead.** `MTFAggregator` keeps `closed(tf)` (safe for signals) and `forming(tf)` (partial bar to cursor, what the chart draws). A HTF bar closes only when the base bar that ends it has been emitted. Higher-TF indicator values must be computed on `closed` bars (or `forming` explicitly flagged). Daily buckets align to the 18:00 NY futures day.
- **Sessions.** Defined in `America/New_York` wall-clock (DST-safe): Asia 20:00–00:00, London 02:00–05:00, NY AM 09:30–12:00, NY PM 13:30–16:00. Session ranges (e.g. Asian high/low) are running aggregates updated per revealed bar, so overlays grow with the replay instead of being pre-drawn. Make windows user-configurable.

## 3. Execution and account simulation

State machine: `PENDING (latency) → WORKING → FILLED | CANCELLED | REJECTED`; a fill creates/reduces a `Position`; SL/TP/trailing live on the position and are evaluated every base bar.

- **Intrabar ambiguity rule (critical).** With OHLC only, if SL and TP are both inside one bar, assume **SL first**. At 1s resolution this is rarely material; at 1m+ it is.
- **Market:** fill at the open of the first bar after latency, ± slippage ticks. **Limit:** requires price to trade 1 tick *through* (queue-position proxy); gaps give price improvement. **Stop:** triggers on touch, fills at worse of open/stop plus slippage.
- **Trailing:** `best` price tracked per position; new SL applies from the *next* bar (no same-bar leak).
- **Partials:** `reduce_only` market order, FIFO against open lots.
- **Costs:** NQ $20/pt, tick $5; MNQ $2/pt. Per-side commission, slippage in ticks, spread ≈ 0–1 tick (futures quote a 1-tick spread; with bar data model it via slippage). Provide a news-window multiplier (2–5× slippage around 08:30/10:00 NY releases).
- **Margin:** reject if `used + new > equity`; use intraday vs overnight margin tables; optional margin-call liquidation at maintenance.
- **Metrics:** equity = balance + unrealized, sampled every revealed bar; max drawdown from running peak; win rate, profit factor, average R (PnL ÷ initial risk), expectancy; Monte Carlo = bootstrap/shuffle the trade list for drawdown/ruin distributions.

## 4. Blueprint

Working reference implementation: [nq_replay.py](nq_replay.py) (Python, stdlib + `tzdata` on Windows). It covers instrument/bar schema, session windows and ranges, no-look-ahead MTF aggregation, broker (market/limit/stop, SL/TP, trailing, partial close, latency, slippage, margin, commissions), replay session (play/pause/seek/step-back with snapshots), and stats. `python nq_replay.py` runs a synthetic self-check, including determinism after step-back.

Suggested production layout:

```
data/     ingest_databento.py -> parquet writer, roll table
engine/   instrument.py bars.py mtf.py sessions.py broker.py replay.py stats.py   (split of nq_replay.py)
api/      FastAPI: GET /bars?symbol&from&to&res (Arrow), WS /session/{id} (commands: play,pause,speed,seek,order)
web/      TypeScript client: lightweight-charts + same cursor/aggregator logic, or thin client over the WS
```

WebSocket protocol sketch: client → `{cmd:"play"|"pause"|"speed"|"seek"|"step"|"order"|"cancel"|"modify"}`; server → `{type:"bar", tf, bar}`, `{type:"fill", ...}`, `{type:"account", equity, margin}`, `{type:"session", name, hi, lo}`.

## Known limitations / next steps
1. OHLC-based fills are approximations; for scalping use `trades`/`mbp-1` data and queue modeling.
2. The reference code is a single file, untested beyond the self-check; add unit tests for gap fills, SL/TP ties, trailing, and DST transitions before relying on results.
3. Verify the data vendor's licence allows redistribution to end users if you build a product.
