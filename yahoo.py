"""
yahoo.py - real NQ futures (NQ=F continuous) 1-minute candles from Yahoo Finance -> daily Parquet.

    python nq_data.py yahoo                  # fetch the last ~7 days (Yahoo's limit for 1-minute data)
    python nq_data.py yahoo --file nqf.json  # import a saved response instead

Yahoo only keeps about 7 days of 1-minute history, so run this every few days and the days accumulate on disk.
The files go under the dataset NQ.yahoo with the usual schema plus a `dur` column (60 s), so the replay
runs on 1-minute candles: timeframes below 1 minute are not available, and fills inside a candle are
approximate. Only complete trading days are stored (the first day in a response is usually cut off at the
start, and the current day is unfinished), because an existing day file is never rewritten by a later run.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pyarrow as pa

import nq_data as nd
from journal import trading_day

SYMBOL = "NQ.yahoo"
BAR_S = 60
URL = "https://query1.finance.yahoo.com/v8/finance/chart/NQ=F?interval=1m&range=7d"
USER_AGENT = "Mozilla/5.0 (compatible; nq-replay personal backtesting)"
SCHEMA = nd.SCHEMA.append(pa.field("dur", pa.int64()))
EDGE_SLACK_S = (
    10 * 60
)  # a day must start within this long of its 18:00 NY open to count as complete

Fetch = Callable[[], bytes]


def fetch_json() -> bytes:
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse(raw: bytes) -> list[tuple[int, float, float, float, float, int]]:
    """(minute start epoch s, o, h, l, c, v); candles Yahoo left empty (None) are dropped."""
    try:
        res = json.loads(raw)["chart"]["result"][0]
        ts, q = res["timestamp"], res["indicators"]["quote"][0]
        cols = [q[k] for k in ("open", "high", "low", "close", "volume")]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ValueError("that is not a Yahoo chart response") from e
    out = []
    for i, t in enumerate(ts):
        o, h, lo, c, v = (col[i] for col in cols)
        if None in (o, h, lo, c):
            continue
        out.append((t - t % BAR_S, float(o), float(h), float(lo), float(c), int(v or 0)))
    return out


def by_trading_day(rows) -> dict[date, list]:
    days: dict[date, list] = {}
    for r in rows:
        days.setdefault(date.fromisoformat(trading_day(r[0])), []).append(r)
    return days


def to_table(rows: list) -> pa.Table:
    rows = sorted(rows)
    cols = list(zip(*rows, strict=True))
    n = len(rows)
    return pa.table(
        {
            "ts": pa.array([t * 10**9 for t in cols[0]], pa.int64()),
            "o": pa.array(cols[1], pa.float64()),
            "h": pa.array(cols[2], pa.float64()),
            "l": pa.array(cols[3], pa.float64()),
            "c": pa.array(cols[4], pa.float64()),
            "v": pa.array(cols[5], pa.int64()),
            "instrument_id": pa.array([0] * n, pa.uint32()),
            "dur": pa.array([BAR_S * 10**9] * n, pa.int64()),
        },
        schema=SCHEMA,
    )


def ingest(
    raw: bytes,
    root: Path = nd.DEFAULT_ROOT,
    force: bool = False,
    say: Callable[[str], None] = print,
) -> int:
    """Store every complete, finished trading day found in `raw`. Returns days written."""
    n = 0
    for d, rows in sorted(by_trading_day(parse(raw)).items()):
        s, e = nd.trading_day_window_utc(d)
        if e + nd.SETTLE > nd._utcnow():
            say(f"{d}: skipped, trading day not finished yet")
            continue
        if min(r[0] for r in rows) > s.timestamp() + EDGE_SLACK_S:
            say(f"{d}: skipped, the response starts part-way through this day")
            continue
        if max(r[0] for r in rows) < e.timestamp() - EDGE_SLACK_S:
            say(f"{d}: skipped, the response stops part-way through this day")
            continue
        p = nd.day_path(root, SYMBOL, d)
        if p.exists() and not force:
            say(f"{d}: already on disk")
            continue
        nd.write_day(to_table(rows), p)
        n += 1
        say(f"{d}: {len(rows):,} candles")
    return n
