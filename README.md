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
