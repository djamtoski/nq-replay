"""
dukascopy.py - free Nasdaq 100 (US100 index CFD) ticks from Dukascopy -> 1-second daily Parquet.

Same files and layout as the Databento ingest (see nq_data.py), under the symbol directory USATECH.IDX, so
the loader, server and UI treat it like any other dataset.

    python nq_data.py dukascopy --start 2024-01-02 --end 2024-12-31

What this data is (and is not)
  * It is the cash-index CFD quote, not the NQ futures contract: prices track NQ closely but differ by the
    futures basis (a few points, drifting), there are no contract rolls, and the CFD trades almost 24h.
    Candle shapes and intraday patterns match; absolute prices do not.
  * Bars are built from the MID price (bid+ask)/2, rounded to the 0.25 tick. `v` is the number of quotes in the
    second, an activity proxy: the feed has no traded volume.
  * Seconds without a quote get no bar (same as Databento's ohlcv-1s).
  * Free for personal use as far as the public feed goes; check Dukascopy's terms before anything else.
  * Size: roughly 2-4 MB of downloads per trading day (about 1 GB per year), ~1-2 MB of Parquet per day.

Feed format: https://datafeed.dukascopy.com/datafeed/<SYMBOL>/<YYYY>/<MM>/<DD>/<HH>h_ticks.bi5 where MM is
ZERO-based (January = 00). Each file is an LZMA stream of 20-byte big-endian records:
(ms since the hour: uint32, ask: uint32, bid: uint32, ask volume: float32, bid volume: float32);
index prices are integers scaled by 1000.
"""

from __future__ import annotations

import lzma
import struct
import time as _time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa

import nq_data as nd

FEED = "https://datafeed.dukascopy.com/datafeed"
FEED_SYMBOL = "USATECHIDXUSD"
SYMBOL = "USATECH.IDX"  # directory name under data/bars
PRICE_SCALE = 1000.0
TICK = 0.25
RECORD = struct.Struct(">IIIff")
WORKERS = 6
RETRIES = 3
USER_AGENT = "nq-replay/1.0 (personal backtesting; contact: local)"

Fetch = Callable[
    [datetime], bytes | None
]  # hour start (UTC) -> raw .bi5 bytes, or None if the feed has none


def hour_url(h: datetime) -> str:
    """Note the zero-based month: March is 02."""
    return f"{FEED}/{FEED_SYMBOL}/{h.year}/{h.month - 1:02d}/{h.day:02d}/{h.hour:02d}h_ticks.bi5"


def fetch_hour(h: datetime, *, opener=urllib.request.urlopen, sleep=_time.sleep) -> bytes | None:
    """Download one hour. 404 means the market was closed (None); any other failure is retried and then
    raised, so a flaky connection can never silently turn into a day with a hole in it."""
    req = urllib.request.Request(hour_url(h), headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with opener(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        sleep(1.5 * (attempt + 1))
    raise ConnectionError(f"could not download {h:%Y-%m-%d %H}h after {RETRIES} tries: {last}")


def decode_ticks(raw: bytes) -> list[tuple[int, float]]:
    """(ms since the hour, mid price) for every quote in one hourly file."""
    if not raw:
        return []
    data = lzma.decompress(raw)
    if len(data) % RECORD.size:
        raise ValueError("tick file is truncated or not in the expected format")
    return [
        (ms, (ask + bid) / 2 / PRICE_SCALE) for ms, ask, bid, _av, _bv in RECORD.iter_unpack(data)
    ]


def _tick_round(p: float) -> float:
    return round(p / TICK) * TICK


def quotes_to_bars(hour_start_s: int, quotes: list[tuple[int, float]]) -> list[list]:
    """1-second [ts_ns, o, h, l, c, count] rows from one hour of quotes (assumed time-ordered)."""
    bars: dict[int, list] = {}
    for ms, px in quotes:
        sec = hour_start_s + ms // 1000
        b = bars.get(sec)
        if b is None:
            bars[sec] = [px, px, px, px, 1]
        else:
            b[1] = max(b[1], px)
            b[2] = min(b[2], px)
            b[3] = px
            b[4] += 1
    return [
        [
            s * 10**9,
            _tick_round(b[0]),
            _tick_round(b[1]),
            _tick_round(b[2]),
            _tick_round(b[3]),
            b[4],
        ]
        for s, b in sorted(bars.items())
    ]


def hours_in(s: datetime, e: datetime) -> list[datetime]:
    h = s.replace(minute=0, second=0, microsecond=0)
    out = []
    while h < e:
        out.append(h)
        h += timedelta(hours=1)
    return out


def build_day(d: date, fetch: Fetch = fetch_hour, workers: int = WORKERS) -> pa.Table | None:
    """All 1s bars of CME trading day `d` (18:00 NY prior evening -> 17:00 NY), or None if the feed was empty."""
    s, e = nd.trading_day_window_utc(d)
    hrs = hours_in(s, e)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        files = list(pool.map(fetch, hrs))  # map keeps order and re-raises the first failure
    lo, hi = int(s.timestamp()) * 10**9, int(e.timestamp()) * 10**9
    rows: list[list] = []
    for h, raw in zip(hrs, files, strict=True):
        if raw is None:
            continue
        rows += [
            r for r in quotes_to_bars(int(h.timestamp()), decode_ticks(raw)) if lo <= r[0] < hi
        ]
    if not rows:
        return None
    cols = list(zip(*rows, strict=True))
    return pa.table(
        {
            "ts": pa.array(cols[0], pa.int64()),
            "o": pa.array(cols[1], pa.float64()),
            "h": pa.array(cols[2], pa.float64()),
            "l": pa.array(cols[3], pa.float64()),
            "c": pa.array(cols[4], pa.float64()),
            "v": pa.array(cols[5], pa.int64()),
            "instrument_id": pa.array([0] * len(rows), pa.uint32()),
        },
        schema=nd.SCHEMA,
    ).sort_by("ts")


def ingest(
    start: date,
    end: date,
    root: Path = nd.DEFAULT_ROOT,
    force: bool = False,
    fetch: Fetch = fetch_hour,
    say: Callable[[str], None] = print,
) -> int:
    """Download missing, finished trading days. Resumable: existing files are skipped unless force.
    Returns the number of days written. A failed download stops the run; days already written are kept."""
    plan = nd.plan_ingest(SYMBOL, start, end, root, force)
    for d in plan.unsettled:
        say(f"{d}: skipped, trading day not finished yet")
    n = 0
    for d in plan.todo:
        table = build_day(d, fetch)
        if table is None:
            say(f"{d}: no data (weekend or holiday)")
            continue
        s, e = nd.trading_day_window_utc(d)
        nd._check_window(table, s, e, d)
        path = nd.day_path(root, SYMBOL, d)
        nd.write_day(table, path)
        n += 1
        say(f"{d}: {table.num_rows:,} bars")
    return n


def estimate(
    start: date, end: date, root: Path = nd.DEFAULT_ROOT, force: bool = False
) -> tuple[int, int, float]:
    """(days to download, days already on disk, rough download size in MB). Free, but not small."""
    plan = nd.plan_ingest(SYMBOL, start, end, root, force)
    return len(plan.todo), plan.have, len(plan.todo) * 3.0
