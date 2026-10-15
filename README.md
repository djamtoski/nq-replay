# NQ replay / backtest

| File | Purpose |
|---|---|
| `nq_replay.py` | Engine: instruments, sessions, no-look-ahead MTF bars, broker, replay session |
| `nq_data.py` | Databento `ohlcv-1s` -> daily Parquet; loader into `ReplaySession` |
| `server.py` | FastAPI + WebSocket API around `ReplaySession` |
| `web/` | Vite + TypeScript + lightweight-charts frontend |
| `BREAKDOWN.md` | Design write-up (public docs + standard engine design; FXReplay internals were not analyzed) |

## Run (two terminals)
```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000
cd web && npm install && npm run dev      # http://127.0.0.1:5173
```
Frontend and API only accept `localhost`/`127.0.0.1` origins (see `ALLOWED_ORIGINS` in `server.py`).

## Real data (optional, costs money)
```bash
set DATABENTO_API_KEY=...                 # never commit this
python nq_data.py cost   --start 2025-03-03 --end 2025-03-07
python nq_data.py ingest --start 2025-03-03 --end 2025-03-07
```
Then choose "Parquet" in the UI.

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

## Tests
```bash
python -m pytest -q          # engine, data, server
cd web && npm test && npx tsc --noEmit
```

## Drawing tools
Left toolbar: trend line, ray, arrow, horizontal line/ray, vertical line, rectangle, ellipse, parallel
channel, Fib retracement, long/short position (target, stop, R:R, $), measure, text, brush.
Click-click or press-drag to draw; click a drawing to select, drag its body or handles; `Del` deletes,
`Ctrl+Z` undoes, `Esc` cancels. Drawings are stored in time/price (not bar index), so they stay put across
timeframes and can sit in the empty space right of "now". They persist per data source + contract in
`localStorage` (`drawings:v1:<source>:<instrument>`). This is a core set, not all of TradingView's tools.

## Rules to keep replays correct
* Orders/cancels/modifies/closes go through `ReplaySession.submit/cancel/modify/close`, never `session.broker.*`,
  so `step_back` can replay them. Strategy callbacks must follow the same rule.
* `step_back` has undo semantics: actions after the target are discarded.
