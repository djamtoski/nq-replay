"""
nq_data.py - Databento ohlcv-1s -> daily Parquet, plus a loader that feeds ReplaySession.

Layout (one file per CME *trading day*, 18:00 NY prior evening -> 17:00 NY):
    <root>/<SYMBOL>/1s/<YYYY>/<YYYY-MM-DD>.parquet
    columns: ts int64 (bar open, epoch ns UTC) | o,h,l,c float64 | v int64 | instrument_id uint32

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
import re
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from nq_replay import NQ, Bar, Instrument, ReplaySession

NY = ZoneInfo("America/New_York")
DATASET = "GLBX.MDP3"
DEFAULT_ROOT = Path(__file__).parent / "data" / "bars"
SETTLE = timedelta(hours=2)  # a trading day is only downloaded this long after its 17:00 NY close
DEFAULT_YES_LIMIT_USD = 25.0  # --yes without --max-cost refuses estimates above this
# One rule, reused by the API's request model. No lookahead (pydantic's regex engine has none): a '.' must be
# followed by a normal character, which rules out '..', a leading/trailing dot and so any path escape.
SYMBOL_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9_-]|\.[A-Za-z0-9_-])*$"
SYMBOL_MAX = 32
SYMBOL_RE = re.compile(SYMBOL_PATTERN)
SCHEMA = pa.schema(
    [
        ("ts", pa.int64()),
        ("o", pa.float64()),
        ("h", pa.float64()),
        ("l", pa.float64()),
        ("c", pa.float64()),
        ("v", pa.int64()),
        ("instrument_id", pa.uint32()),  # Databento ids are uint32 (can exceed 2**31)
    ]
)


# ------------------------------------------------------------------ paths / calendar
def _utcnow() -> datetime:
    return datetime.now(UTC)  # one seam so tests can pin "now"


def check_symbol(symbol: str) -> str:
    """Symbols become directory names, so reject anything that could escape the data root."""
    if len(symbol) > SYMBOL_MAX or not SYMBOL_RE.fullmatch(symbol):
        raise ValueError(
            f"invalid symbol {symbol!r}: use letters, digits, '.', '_' or '-' (e.g. NQ.v.0)"
        )
    return symbol


def day_path(root: Path, symbol: str, d: date) -> Path:
    # The FULL symbol is the directory: NQ.v.0 (volume roll), NQ.c.0 (calendar roll) and NQ.v.1 are different series
    return Path(root) / check_symbol(symbol) / "1s" / f"{d.year}" / f"{d.isoformat()}.parquet"


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
    return s.astimezone(UTC), e.astimezone(UTC)


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
    ts = idx.as_unit("ns").asi8  # pandas may hold us/ms resolution; the schema is always ns
    if len(ts) and not (1e18 < ts[0] < 4e18):
        raise ValueError(f"timestamp {ts[0]} is not epoch-nanoseconds in a plausible range")
    t = pa.table(
        {
            "ts": pa.array(ts, pa.int64()),
            "o": pa.array(df["open"].to_numpy(), pa.float64()),
            "h": pa.array(df["high"].to_numpy(), pa.float64()),
            "l": pa.array(df["low"].to_numpy(), pa.float64()),
            "c": pa.array(df["close"].to_numpy(), pa.float64()),
            "v": pa.array(df["volume"].to_numpy(), pa.int64()),
            "instrument_id": pa.array(df["instrument_id"].to_numpy(), pa.uint32()),
        },
        schema=SCHEMA,
    )
    return t.sort_by("ts")


def write_day(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # unique temp name: two ingests of the same day (two terminals) must not write the same temp file
    tmp = path.with_name(f"{path.stem}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        pq.write_table(table, tmp, compression="zstd", row_group_size=3600)
        tmp.replace(path)  # atomic: a crash never leaves a half-written day behind
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ planning (what would actually be downloaded)
@dataclass(frozen=True)
class Plan:
    todo: list[date]  # settled trading days with no file (or all of them with force)
    have: int  # already on disk (skipped)
    unsettled: list[date]  # trading day not finished + SETTLE ago: data may still be incomplete


def plan_ingest(
    symbol: str, start: date, end: date, root: Path = DEFAULT_ROOT, force: bool = False
) -> Plan:
    """
    The single source of truth for both the cost estimate and the download, so the price shown is for the days
    that will really be fetched. A day still open (or just closed) is never stored: it would be partial, and
    because existing files are skipped on later runs it would stay partial forever.
    """
    now = _utcnow()
    todo: list[date] = []
    unsettled: list[date] = []
    have = 0
    for d in trading_days(start, end):
        if trading_day_window_utc(d)[1] + SETTLE > now:
            unsettled.append(d)
        elif day_path(root, symbol, d).exists() and not force:
            have += 1
        else:
            todo.append(d)
    return Plan(todo, have, unsettled)


def _next_trading_day(d: date) -> date:
    n = d + timedelta(days=1)
    return n + timedelta(days=7 - n.weekday()) if n.weekday() >= 5 else n


def _runs(days: list[date]) -> list[list[date]]:
    """Group consecutive trading days so each priced request is one contiguous window."""
    out: list[list[date]] = []
    for d in days:
        if out and _next_trading_day(out[-1][-1]) == d:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def cost(
    symbol: str, start: date, end: date, root: Path = DEFAULT_ROOT, force: bool = False
) -> float:
    """USD estimate for the days ingest() would download (not the whole range)."""
    client = _client()
    total = 0.0
    for run in _runs(plan_ingest(symbol, start, end, root, force).todo):
        s, _ = trading_day_window_utc(run[0])
        _, e = trading_day_window_utc(run[-1])
        total += client.metadata.get_cost(
            dataset=DATASET,
            symbols=[symbol],
            stype_in="continuous",
            schema="ohlcv-1s",
            start=s.isoformat(),
            end=e.isoformat(),
        )
    return total


def _check_window(table: pa.Table, s: datetime, e: datetime, d: date) -> None:
    """Refuse to file bars under a day they don't belong to (would show up later as overlapping duplicates)."""
    mm = pc.min_max(table["ts"]).as_py()
    lo, hi = int(s.timestamp()) * 10**9, int(e.timestamp()) * 10**9
    if mm["min"] < lo or mm["max"] >= hi:
        raise ValueError(
            f"{d}: the API returned bars outside the requested window "
            f"{s.isoformat()} .. {e.isoformat()}; nothing was written for this day"
        )


def ingest(
    symbol: str, start: date, end: date, root: Path = DEFAULT_ROOT, force: bool = False
) -> int:
    """Download missing, finished trading days. Resumable: existing files are skipped unless force."""
    check_symbol(symbol)
    client, n = _client(), 0
    plan = plan_ingest(symbol, start, end, root, force)
    for d in plan.unsettled:
        print(
            f"{d}: skipped, trading day not finished yet (available {SETTLE} after its 17:00 NY close)"
        )
    for d in plan.todo:
        p = day_path(root, symbol, d)
        s, e = trading_day_window_utc(d)
        store = client.timeseries.get_range(
            dataset=DATASET,
            symbols=[symbol],
            stype_in="continuous",
            schema="ohlcv-1s",
            start=s.isoformat(),
            end=e.isoformat(),
        )
        df = store.to_df(price_type="float", pretty_ts=True)
        if df.empty:
            print(f"{d}: no data (holiday?)")
            continue
        table = frame_to_table(df)
        _check_window(table, s, e, d)
        write_day(table, p)
        n += 1
        print(f"{d}: {len(df):,} bars -> {p}")
    return n


# ------------------------------------------------------------------ load
def load_bars(
    symbol: str, start: date, end: date, root: Path = DEFAULT_ROOT, back_adjust: bool = False
) -> list[Bar]:
    """Read daily files for trading dates [start, end] into Bar objects (1s)."""
    files = [day_path(root, symbol, d) for d in trading_days(start, end)]
    files = [f for f in files if f.exists()]
    if not files:
        raise FileNotFoundError(f"no Parquet files for {symbol} {start}..{end} under {root}")
    tbl = pa.concat_tables([pq.read_table(f) for f in files]).sort_by("ts")
    ts, o, h, l, c, v, iid = (
        tbl.column(n).to_pylist() for n in ("ts", "o", "h", "l", "c", "v", "instrument_id")
    )
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
    return (
        [x + k for x, k in zip(o, offset)],
        [x + k for x, k in zip(h, offset)],
        [x + k for x, k in zip(l, offset)],
        [x + k for x, k in zip(c, offset)],
    )


def load_session(
    symbol: str,
    start: date,
    end: date,
    root: Path = DEFAULT_ROOT,
    inst: Instrument = NQ,
    back_adjust: bool = False,
    **session_kw,
) -> ReplaySession:
    return ReplaySession(load_bars(symbol, start, end, root, back_adjust), inst, **session_kw)


def info(root: Path = DEFAULT_ROOT) -> None:
    for f in sorted(Path(root).glob("*/1s/*/*.parquet")):
        m = pq.read_metadata(f)
        print(f"{f.relative_to(root)}  rows={m.num_rows:,}  {f.stat().st_size / 1e6:.2f} MB")


def _d(s: str) -> date:
    return date.fromisoformat(s)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=["cost", "ingest", "info"])
    ap.add_argument("--symbol", default="NQ.v.0")
    ap.add_argument("--start", type=_d)
    ap.add_argument("--end", type=_d)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    ap.add_argument(
        "--max-cost",
        type=float,
        metavar="USD",
        help=f"refuse to download if the estimate is above this (with --yes the default is ${DEFAULT_YES_LIMIT_USD:.0f})",
    )
    a = ap.parse_args(argv)
    if a.cmd == "info":
        return info(a.root)
    if not (a.start and a.end):
        ap.error("--start and --end are required")
    try:
        check_symbol(a.symbol)
    except ValueError as e:
        ap.error(str(e))
    import databento as db  # lazy: the loader/engine must stay importable without the API client

    try:
        plan = plan_ingest(a.symbol, a.start, a.end, a.root, a.force)
        print(
            f"{len(plan.todo)} trading day(s) to download, {plan.have} already on disk"
            + (f", {len(plan.unsettled)} not finished yet (skipped)" if plan.unsettled else "")
        )
        if not plan.todo:
            return print("nothing to download")
        usd = cost(a.symbol, a.start, a.end, a.root, a.force)
        print(
            f"Estimated Databento cost: ${usd:,.2f} for {a.symbol} (the {len(plan.todo)} day(s) above)"
        )
        if a.cmd == "cost":
            return
        limit = a.max_cost if a.max_cost is not None else (DEFAULT_YES_LIMIT_USD if a.yes else None)
        if limit is not None and usd > limit:
            sys.exit(
                f"Estimated ${usd:,.2f} is above the ${limit:,.2f} limit; re-run with --max-cost {usd:,.0f} (or more) to allow it."
            )
        if not a.yes:
            try:
                answer = input("Proceed? [y/N] ")
            except EOFError:  # non-interactive (CI, redirected stdin): never proceed silently
                answer = ""
            if answer.strip().lower() != "y":
                return print("aborted")
        print(f"done: {ingest(a.symbol, a.start, a.end, a.root, a.force)} day(s) written")
    except db.BentoError as e:
        # bad key / quota / dates the service doesn't have yet: a clear message, not a traceback.
        # Days already written stay on disk and the next run resumes after them.
        sys.exit(
            f"Databento rejected the request: {e}\nDays already downloaded are kept; re-run to resume."
        )


if __name__ == "__main__":
    main()
