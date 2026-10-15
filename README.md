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

## Tests
```bash
python -m pytest -q          # engine, data, server
cd web && npm test && npx tsc --noEmit
```

## Rules to keep replays correct
* Orders/cancels/modifies/closes go through `ReplaySession.submit/cancel/modify/close`, never `session.broker.*`,
  so `step_back` can replay them. Strategy callbacks must follow the same rule.
* `step_back` has undo semantics: actions after the target are discarded.
