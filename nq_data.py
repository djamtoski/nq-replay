"""
nq_data.py - Databento ohlcv-1s -> daily Parquet, plus a loader that feeds ReplaySession.

Layout (one file per CME *trading day*, 18:00 NY prior evening -> 17:00 NY):
    <root>/<SYMBOL>/1s/<YYYY>/<YYYY-MM-DD>.parquet
    columns: ts int64 (bar open, epoch ns UTC) | o,h,l,c float64 | v int64 | instrument_id int32

CLI (reads DATABENTO_API_KEY from the environment; the key is never logged or stored):
    python nq_data.py cost   --start 2025-03-03 --end 2025-03-08
    python nq_data.py ingest --start 2025-03-03 --end 2025-03-08 [--yes]
    python nq_data.py info
Notes:
  * ohlcv-1s only emits a bar when a trade occurred; gaps are real and are not forward-filled.
  * Default symbol NQ.v.0 = volume-ranked front month (rolls on volume, not calendar).
    Prices are NOT back-adjusted at write time; use load_session(..., back_adjust=True).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from nq_replay import NQ, Bar, ExecModel, Instrument, ReplaySession

NY = ZoneInfo("America/New_York")
DATASET = "GLBX.MDP3"
DEFAULT_ROOT = Path(__file__).parent / "data" / "bars"
SCHEMA = pa.schema([
    ("ts", pa.int64()), ("o", pa.float64()), ("h", pa.float64()), ("l", pa.float64()),
    ("c", pa.float64()), ("v", pa.int64()), ("instrument_id", pa.int32()),
])


# ------------------------------------------------------------------ paths / calendar
def day_path(root: Path, symbol: str, d: date) -> Path:
    return Path(root) / symbol.split(".")[0] / "1s" / f"{d.year}" / f"{d.isoformat()}.parquet"


def trading_days(start: date, end: date) -> Iterable[date]:
    """Mon-Fri trading dates in [start, end]. Exchange holidays simply come back empty."""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def trading_day_window_utc(d: date) -> tuple[datetime, datetime]:
    """Trading date D runs 18:00 NY on the prior calendar day to 17:00 NY on D (Mon starts Sun 18:00)."""
    prev = d - timedelta(days=1)
    s = datetime.combine(prev, time(18, 0), tzinfo=NY)
    e = datetime.combine(d, time(17, 0), tzinfo=NY)
    return s.astimezone(timezone.utc), e.astimezone(timezone.utc)


# ------------------------------------------------------------------ write
def _client():
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("DATABENTO_API_KEY is not set in the environment.")
    import databento as db
    return db.Historical(key)


def frame_to_table(df) -> pa.Table:
    """Convert a Databento ohlcv-1s DataFrame (index ts_event UTC) to our schema."""
    idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    ts = idx.as_unit("ns").asi8          # pandas may hold us/ms resolution; the schema is always ns
    if len(ts) and not (1e18 < ts[0] < 4e18):
        raise ValueError(f"timestamp {ts[0]} is not epoch-nanoseconds in a plausible range")
    t = pa.table({
        "ts": pa.array(ts, pa.int64()),
        "o": pa.array(df["open"].to_numpy(), pa.float64()),
        "h": pa.array(df["high"].to_numpy(), pa.float64()),
        "l": pa.array(df["low"].to_numpy(), pa.float64()),
        "c": pa.array(df["close"].to_numpy(), pa.float64()),
        "v": pa.array(df["volume"].to_numpy(), pa.int64()),
        "instrument_id": pa.array(df["instrument_id"].to_numpy(), pa.int32()),
    }, schema=SCHEMA)
    return t.sort_by("ts")


def write_day(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd", row_group_size=3600)
    tmp.replace(path)  # atomic: a crash never leaves a half-written day behind


def cost(symbol: str, start: date, end: date) -> float:
    s, _ = trading_day_window_utc(start)
    _, e = trading_day_window_utc(end)
    return _client().metadata.get_cost(
        dataset=DATASET, symbols=[symbol], stype_in="continuous", schema="ohlcv-1s",
        start=s.isoformat(), end=e.isoformat())


def ingest(symbol: str, start: date, end: date, root: Path = DEFAULT_ROOT, force: bool = False) -> int:
    """Download missing trading days. Resumable: existing files are skipped unless force."""
    client, n = _client(), 0
    for d in trading_days(start, end):
        p = day_path(root, symbol, d)
        if p.exists() and not force:
            continue
        s, e = trading_day_window_utc(d)
        store = client.timeseries.get_range(
            dataset=DATASET, symbols=[symbol], stype_in="continuous", schema="ohlcv-1s",
            start=s.isoformat(), end=e.isoformat())
        df = store.to_df(price_type="float", pretty_ts=True)
        if df.empty:
            print(f"{d}: no data (holiday?)")
            continue
        write_day(frame_to_table(df), p)
        n += 1
        print(f"{d}: {len(df):,} bars -> {p}")
    return n


# ------------------------------------------------------------------ load
def load_bars(symbol: str, start: date, end: date, root: Path = DEFAULT_ROOT,
              back_adjust: bool = False) -> list[Bar]:
    """Read daily files for trading dates [start, end] into Bar objects (1s)."""
    files = [day_path(root, symbol, d) for d in trading_days(start, end)]
    files = [f for f in files if f.exists()]
    if not files:
        raise FileNotFoundError(f"no Parquet files for {symbol} {start}..{end} under {root}")
    tbl = pa.concat_tables([pq.read_table(f) for f in files]).sort_by("ts")
    ts, o, h, l, c, v, iid = (tbl.column(n).to_pylist() for n in ("ts", "o", "h", "l", "c", "v", "instrument_id"))
    if back_adjust:
        o, h, l, c = _back_adjust(o, h, l, c, iid)
    D = 10**9
    return [Bar(ts[i], o[i], h[i], l[i], c[i], v[i], D) for i in range(len(ts))]


def _back_adjust(o, h, l, c, iid):
    """
    Difference back-adjustment: at each instrument_id change, shift every earlier price by
    (first open of new contract - last close of old contract). Latest contract stays unadjusted,
    so recent prices are real tradable levels. Rounded to the 0.25 tick.
    """
    n = len(c)
    offset, off = [0.0] * n, 0.0
    for i in range(n - 1, 0, -1):
        offset[i] = off
        if iid[i] != iid[i - 1]:
            off += round((o[i] - c[i - 1]) * 4) / 4
    offset[0] = off
    return ([x + k for x, k in zip(o, offset)], [x + k for x, k in zip(h, offset)],
            [x + k for x, k in zip(l, offset)], [x + k for x, k in zip(c, offset)])


def load_session(symbol: str, start: date, end: date, root: Path = DEFAULT_ROOT,
                 inst: Instrument = NQ, back_adjust: bool = False, **session_kw) -> ReplaySession:
    return ReplaySession(load_bars(symbol, start, end, root, back_adjust), inst, **session_kw)


def info(root: Path = DEFAULT_ROOT) -> None:
    for f in sorted(Path(root).glob("*/1s/*/*.parquet")):
        m = pq.read_metadata(f)
        print(f"{f.relative_to(root)}  rows={m.num_rows:,}  {f.stat().st_size / 1e6:.2f} MB")


def _d(s: str) -> date:
    return date.fromisoformat(s)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["cost", "ingest", "info"])
    ap.add_argument("--symbol", default="NQ.v.0")
    ap.add_argument("--start", type=_d)
    ap.add_argument("--end", type=_d)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    a = ap.parse_args(argv)
    if a.cmd == "info":
        return info(a.root)
    if not (a.start and a.end):
        ap.error("--start and --end are required")
    usd = cost(a.symbol, a.start, a.end)
    print(f"Estimated Databento cost: ${usd:,.2f} for {a.symbol} {a.start}..{a.end}")
    if a.cmd == "cost":
        return
    if not a.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        return print("aborted")
    print(f"done: {ingest(a.symbol, a.start, a.end, a.root, a.force)} day(s) written")


if __name__ == "__main__":
    main()
