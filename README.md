# NQ replay / backtest

| File | Purpose |
|---|---|
| `nq_replay.py` | Engine: instruments, sessions, no-look-ahead MTF bars, broker, replay session |
| `nq_data.py` | Databento `ohlcv-1s` -> daily Parquet; loader into `ReplaySession` |
| `dukascopy.py` | Free Nasdaq 100 CFD ticks -> the same daily 1s Parquet files |
| `server.py` | FastAPI + WebSocket API around `ReplaySession` |
| `web/` | Vite + TypeScript + lightweight-charts frontend |
| `BREAKDOWN.md` | Design write-up (public docs + standard engine design; FXReplay internals were not analyzed) |

## Run (two terminals)
```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
cd web && npm install && npm run dev      # http://127.0.0.1:5173
```
Frontend and API only accept `localhost`/`127.0.0.1` origins (see `ALLOWED_ORIGINS` in `server.py`).

## Real data
Two sources write the same daily files (`data/bars/<dataset>/1s/...`), and the New session dialog lists whatever is
on disk with its date range:

**Free: Nasdaq 100 index CFD from Dukascopy** (no account, no key). Tick quotes become 1-second candles
(mid price, 0.25 tick; volume = number of quotes, since there is no traded volume):
```bash
python nq_data.py dukascopy --start 2024-01-02 --end 2024-12-31    # about 1 GB of downloads per year, resumable
```
It is the cash-index CFD, not the NQ futures contract: candle shapes and intraday patterns match, absolute prices
differ by a few points of basis, there are no contract rolls, and it trades almost 24h. Fine for practising and
backtesting price action; not an exact NQ tape. Check Dukascopy's terms before using it for anything but personal use.

**Paid: Databento NQ futures** (below). In the dialog choose Real data, pick the dataset and dates. The replay
starts on the first day you pick, at the start time, with 3 trading days of earlier data as chart history.
Synthetic data takes a date range too (up to 10 trading days).

## Databento (optional, costs money)
```bash
set DATABENTO_API_KEY=...                 # never commit this
python nq_data.py cost   --start 2025-03-03 --end 2025-03-07
python nq_data.py ingest --start 2025-03-03 --end 2025-03-07
```
Then choose "Real data" and the Databento dataset in the New session dialog. If the dates you pick aren't downloaded yet it says
"No data for <start> to <end>" (the folder and symbol are in the server log, not the dialog), and the replay you
had open stays as it was.

How ingest behaves (all covered by `test_nq_data_databento.py`):
- Files live under the **full symbol** (`data/bars/NQ.v.0/1s/...`), so `NQ.v.0` and `NQ.c.0` never mix.
- Only **finished** trading days (2 h after the 17:00 NY close) are downloaded: a day still open would be stored
  partial and then skipped forever by later runs.
- `cost` and `ingest` both use the same plan, so the price shown is for the days that will really be fetched
  (days already on disk are skipped; `--force` redownloads).
- `--yes` refuses estimates above $25 unless you pass `--max-cost USD`; interactive runs ask first and also
  honour `--max-cost`. Non-interactive runs without `--yes` abort instead of proceeding.
- Bars outside the requested trading-day window are rejected before anything is written.

**First live run (cheap and safe):** the Databento path is tested offline against genuine DBN data (see
`test_nq_data_databento.py`) but has never called the live API. Start small:
1. `python nq_data.py cost --start 2025-03-10 --end 2025-03-10` (one day) and check the price;
2. `python nq_data.py ingest --start 2025-03-10 --end 2025-03-10`, then `python nq_data.py info`;
3. choose "Real data", start/end date 2025-03-10 (the app loads 3 earlier trading days as chart history, so
   ingest those too, or set the start a few days after your first downloaded day).
If Databento rejects a request (bad key, dates it does not have yet) you get a one-line message; days already
downloaded are kept and the next run resumes after them. The key is read only from `DATABENTO_API_KEY`.

## WebSocket protocol (`/ws/{session_id}?tf=<seconds>`)
Bar data is sent **per client, for one timeframe only**: the one in `?tf=` (default 60 if the session has it,
else its first timeframe). Switch with `{"cmd":"view","tf":300}`; the server replies `{"type":"view","tf",
"history":{"300":[...]},"forming":{"300":{...}}}` and ticks then carry only that timeframe.

| Server -> client | Notes |
|---|---|
| `reset` | on connect and after a step back: `tf`, `history`/`forming` for that tf, positions, orders, trades, account |
| `view` | reply to `view`: new timeframe's history and forming bar |
| `tick` | `tf`, `closed` (new bars of that tf only), `forming`, positions, orders, `new_trades`, account, sessions |
| `state` / `end` / `order_ack` / `error` | same for every client |

Client -> server commands: `play`, `pause`, `speed`, `step`, `step_back`, `seek`, `order`, `cancel`, `modify`,
`close`, `view`. A message built for a timeframe the user has since left can still arrive; clients should not
repaint the chart from it (see `drawsChart` in `web/src/store.ts`).

## Tests and checks
```bash
python -m pytest -q          # engine, data, server
cd web && npm test && npx tsc --noEmit
```

CI runs the checks below on every PR (plus dependency audits, CodeQL and weekly Dependabot updates):
```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && mypy nq_replay.py nq_data.py server.py journal.py dukascopy.py
python -m pytest -q --cov                # fails under 80% coverage (pyproject.toml)
cd web && npm run lint && npm run format:check && npm run coverage   # coverage thresholds in vitest.config.ts
```

## Drawing tools
Left toolbar: trend line, ray, arrow, horizontal line/ray, vertical line, rectangle, ellipse, parallel
channel, Fib retracement, long/short position (target, stop, R:R, $), measure, text, brush.
Click-click or press-drag to draw; click a drawing to select, drag its body or handles; `Del` deletes,
`Ctrl+Z` undoes, `Esc` cancels. Drawings are stored in time/price (not bar index), so they stay put across
timeframes and can sit in the empty space right of "now". They persist per data source + contract in
`localStorage` (`drawings:v1:<source>:<instrument>`). This is a core set, not all of TradingView's tools.

## Trade journal (UI)
The **Journal** button in the header opens a full-screen journal with three tabs and shared filters
(date range, setup, side):
- **Trades**: every closed trade, newest first. Select one to see entry/exit, hold time, R, exit reason and
  MAE/MFE in dollars, then add a setup, an emotion, a 1-5 rating, mistake tags and free-text notes (tags
  autocomplete from what you used before). Edits save automatically; **Replay this trade** jumps the chart to
  10 minutes before the entry (reusing the live session, or recreating the stored one); **Delete** removes it.
- **Analytics**: net P&L, win rate, profit factor, expectancy, avg R, drawdown, hold time, avg MAE/MFE, a
  cumulative P&L curve, and tables by setup, mistake, emotion, entry hour (New York), weekday, side and exit.
- **Calendar**: a month grid of daily P&L (green/red, shaded by size); click a day to list its trades.

Not built yet: chart screenshots attached to trades.

## Trade journal (backend)
Every closed trade of every session is mirrored into a local SQLite file, `data/journal.sqlite`
(override with `NQ_JOURNAL=path`; git-ignored). Nothing leaves your machine. Trades are keyed by content, so
stepping back removes trades that no longer exist in the replay, except ones you wrote notes on: those are
hidden and return if you replay the same moves. Deleting a trade in the journal is permanent.

| Endpoint | Purpose |
|---|---|
| `GET /journal/trades` | list, newest first. Filters: `session`, `start`, `end` (trading day), `setup`, `side`; `limit`, `offset` |
| `GET/PATCH/DELETE /journal/trades/{id}` | one trade; PATCH takes `setup`, `emotion`, `rating` (1-5), `mistakes` (tags), `note` |
| `GET /journal/trades/{id}/replay` | the session request + entry time, to jump to the trade (reuse the live session or recreate it) |
| `GET /journal/analytics` | summary (win rate, profit factor, expectancy, avg R, drawdown, MAE/MFE), equity curve, breakdowns by hour / weekday / side / setup / emotion / mistake / exit reason |
| `GET /journal/calendar?year=&month=` | per-trading-day P&L, trades and green/red day counts |
| `GET /journal/tags` | tags already used, for autocomplete |

Days are CME trading days (the 18:00 New York open belongs to the next day). MAE/MFE are in points and bar-level:
a bar's high/low while the position was open, since the order of high and low inside a 1-second bar is unknown.

## Rules to keep replays correct
* Orders/cancels/modifies/closes go through `ReplaySession.submit/cancel/modify/close`, never `session.broker.*`,
  so `step_back` can replay them. Strategy callbacks must follow the same rule.
* `step_back` has undo semantics: actions after the target are discarded.
