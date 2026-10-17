"""Dukascopy ingest, tested offline with hand-built .bi5 files (LZMA streams of 20-byte big-endian records)."""

import lzma
import struct
import urllib.error
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest

import dukascopy as dk
import nq_data as nd

REC = struct.Struct(">IIIff")


def bi5(quotes):
    """quotes: [(ms, bid, ask)] in index points -> compressed hourly file."""
    raw = b"".join(
        REC.pack(ms, round(ask * 1000), round(bid * 1000), 0.1, 0.1) for ms, bid, ask in quotes
    )
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def hour(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=UTC)


class FakeFeed:
    """Serves hourly files from a dict keyed by hour start; anything else is a closed market (None)."""

    def __init__(self, files=None, fail=()):
        self.files, self.fail, self.calls = dict(files or {}), set(fail), []

    def __call__(self, h):
        self.calls.append(h)
        if h in self.fail:
            raise ConnectionError(f"boom {h}")
        return self.files.get(h)


# ---------------------------------------------------------------- url / decode
def test_url_uses_a_zero_based_month():
    assert hour_url_for(2025, 3, 10, 14).endswith("/USATECHIDXUSD/2025/02/10/14h_ticks.bi5")
    assert hour_url_for(2025, 1, 2, 0).endswith("/2025/00/02/00h_ticks.bi5")
    assert hour_url_for(2025, 12, 31, 23).endswith("/2025/11/31/23h_ticks.bi5")


def hour_url_for(y, m, d, h):
    return dk.hour_url(hour(y, m, d, h))


def test_decode_takes_the_mid_of_bid_and_ask_in_index_points():
    q = dk.decode_ticks(bi5([(57, 19756.0, 19758.0), (3_599_882, 19520.0, 19521.0)]))
    assert q == [(57, 19757.0), (3_599_882, 19520.5)]


def test_decode_handles_empty_and_rejects_garbage():
    assert dk.decode_ticks(b"") == []
    with pytest.raises(lzma.LZMAError):
        dk.decode_ticks(b"not lzma at all")
    with pytest.raises(ValueError, match="truncated"):
        dk.decode_ticks(lzma.compress(b"x" * 21, format=lzma.FORMAT_ALONE))


# ---------------------------------------------------------------- bars
def test_quotes_become_one_second_ohlc_with_a_count():
    h0 = int(hour(2025, 3, 10, 14).timestamp())
    q = [(100, 100.0), (400, 101.0), (900, 99.5), (1000, 98.0), (1999, 98.5)]
    bars = dk.quotes_to_bars(h0, q)
    assert bars == [
        [h0 * 10**9, 100.0, 101.0, 99.5, 99.5, 3],
        [(h0 + 1) * 10**9, 98.0, 98.5, 98.0, 98.5, 2],
    ]


def test_prices_snap_to_the_quarter_point_and_seconds_without_quotes_have_no_bar():
    h0 = int(hour(2025, 3, 10, 14).timestamp())
    bars = dk.quotes_to_bars(h0, [(0, 100.1), (5000, 100.14), (5001, 100.13)])
    assert [b[0] // 10**9 - h0 for b in bars] == [0, 5]
    assert bars[0][1:5] == [100.0] * 4
    assert bars[1][1:5] == [100.25] * 4  # 100.14 and 100.13 both snap to 100.25


def test_rounding_is_to_the_nearest_tick():
    assert dk._tick_round(100.124) == 100.0
    assert dk._tick_round(100.126) == 100.25
    assert dk._tick_round(19757.3349) == 19757.25


def test_hours_in_covers_a_partial_first_hour():
    s, e = nd.trading_day_window_utc(date(2025, 3, 11))  # 22:00Z .. 21:00Z (EDT)
    hs = dk.hours_in(s, e)
    assert hs[0] == hour(2025, 3, 10, 22) and hs[-1] == hour(2025, 3, 11, 20) and len(hs) == 23


# ---------------------------------------------------------------- one trading day
D = date(2025, 3, 11)  # Tuesday, EDT: window 2025-03-10 22:00Z .. 2025-03-11 21:00Z


def day_feed():
    return FakeFeed(
        {
            hour(2025, 3, 10, 22): bi5([(0, 100.0, 100.5), (1000, 101.0, 101.5)]),
            hour(2025, 3, 11, 14): bi5([(500, 105.0, 105.5)]),
            hour(2025, 3, 11, 20): bi5([(3_599_000, 110.0, 110.5)]),
            hour(2025, 3, 11, 21): bi5(
                [(0, 120.0, 120.0)]
            ),  # after the 17:00 NY close: not part of the day
        }
    )


def test_build_day_orders_bars_and_stays_inside_the_trading_day_window():
    t = dk.build_day(D, day_feed())
    assert t.schema == nd.SCHEMA
    ts = t["ts"].to_pylist()
    assert ts == sorted(ts) and len(ts) == 4
    s, e = nd.trading_day_window_utc(D)
    assert ts[0] == int(s.timestamp()) * 10**9 and ts[-1] < int(e.timestamp()) * 10**9
    assert t["o"].to_pylist() == [100.25, 101.25, 105.25, 110.25]
    assert set(t["instrument_id"].to_pylist()) == {0}
    assert set(t["v"].to_pylist()) == {1}


def test_build_day_returns_none_when_the_feed_is_empty():
    assert dk.build_day(D, FakeFeed()) is None


def test_a_failed_hour_aborts_the_day_instead_of_leaving_a_hole():
    feed = day_feed()
    feed.fail = {hour(2025, 3, 11, 14)}
    with pytest.raises(ConnectionError):
        dk.build_day(D, feed)


# ---------------------------------------------------------------- ingest
def test_ingest_writes_loadable_daily_files_and_is_resumable(tmp_path):
    out = []
    feed = day_feed()
    n = dk.ingest(date(2025, 3, 11), date(2025, 3, 11), tmp_path, fetch=feed, say=out.append)
    assert n == 1
    p = nd.day_path(tmp_path, dk.SYMBOL, D)
    assert p.exists() and p.parts[-4] == "USATECH.IDX"
    assert pq.read_table(p).num_rows == 4
    assert "4 bars" in out[-1]
    calls = len(feed.calls)
    assert dk.ingest(D, D, tmp_path, fetch=feed, say=out.append) == 0  # already on disk
    assert len(feed.calls) == calls
    bars = nd.load_bars(dk.SYMBOL, D, D, tmp_path)  # the normal loader reads it back
    assert len(bars) == 4 and bars[0].o == 100.25
    assert (
        dk.ingest(D, D, tmp_path, force=True, fetch=feed, say=out.append) == 1
    )  # --force redownloads


def test_ingest_skips_holidays_weekends_and_unfinished_days(tmp_path, monkeypatch):
    out = []
    feed = FakeFeed()  # a market holiday: nothing in any hour
    assert dk.ingest(D, D, tmp_path, fetch=feed, say=out.append) == 0
    assert "no data" in out[-1] and not list(tmp_path.rglob("*.parquet"))
    sat = date(2025, 3, 15)
    assert (
        dk.ingest(sat, sat, tmp_path, fetch=feed, say=out.append) == 0
    )  # weekends are not trading days
    monkeypatch.setattr(
        nd, "_utcnow", lambda: datetime(2025, 3, 11, 20, tzinfo=UTC)
    )  # day not closed yet
    feed2 = day_feed()
    assert dk.ingest(D, D, tmp_path, fetch=feed2, say=out.append) == 0
    assert feed2.calls == [] and "not finished" in out[-1]


def test_a_failure_keeps_earlier_days_and_writes_nothing_for_the_failed_one(tmp_path):
    mon, tue = date(2025, 3, 10), date(2025, 3, 11)
    files = {
        hour(2025, 3, 9, 22): bi5(
            [(0, 90.0, 90.5)]
        ),  # Monday's window starts Sunday 22:00Z (18:00 EDT)
    }
    feed = FakeFeed(files, fail={hour(2025, 3, 11, 14)})
    with pytest.raises(ConnectionError):
        dk.ingest(mon, tue, tmp_path, fetch=feed, say=lambda _m: None)
    assert nd.day_path(tmp_path, dk.SYMBOL, mon).exists()
    assert not nd.day_path(tmp_path, dk.SYMBOL, tue).exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_estimate_counts_only_missing_days(tmp_path):
    dk.ingest(D, D, tmp_path, fetch=day_feed(), say=lambda _m: None)
    todo, have, mb = dk.estimate(date(2025, 3, 10), date(2025, 3, 12), tmp_path)
    assert (todo, have) == (2, 1) and mb == 6.0


# ---------------------------------------------------------------- fetch_hour (network layer, no network)
class Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_fetch_hour_returns_bytes_none_for_404_and_sends_a_user_agent():
    seen = []

    def ok(req, timeout):
        seen.append((req.full_url, req.headers.get("User-agent"), timeout))
        return Resp(b"data")

    assert dk.fetch_hour(hour(2025, 3, 10, 14), opener=ok, sleep=lambda _s: None) == b"data"
    assert seen[0][0].endswith("2025/02/10/14h_ticks.bi5") and "nq-replay" in seen[0][1]

    def missing(req, timeout):
        raise http_error(404)

    assert dk.fetch_hour(hour(2025, 3, 8, 14), opener=missing, sleep=lambda _s: None) is None


def test_fetch_hour_retries_transient_errors_then_gives_up_loudly():
    attempts = []

    def flaky(req, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise http_error(503)
        return Resp(b"ok")

    assert dk.fetch_hour(hour(2025, 3, 10, 14), opener=flaky, sleep=lambda _s: None) == b"ok"
    assert len(attempts) == 3

    def down(req, timeout):
        raise urllib.error.URLError("no route")

    with pytest.raises(ConnectionError, match="could not download 2025-03-10 14h"):
        dk.fetch_hour(hour(2025, 3, 10, 14), opener=down, sleep=lambda _s: None)


def test_a_429_stops_at_once_instead_of_retrying_into_a_longer_block():
    calls = []

    def limited(req, timeout):
        calls.append(1)
        raise http_error(429)

    with pytest.raises(dk.RateLimited, match="HTTP 429"):
        dk.fetch_hour(
            hour(2025, 3, 10, 14), opener=limited, sleep=lambda _s: pytest.fail("must not wait")
        )
    assert len(calls) == 1
    assert issubclass(dk.RateLimited, ConnectionError)  # the CLI already reports these cleanly
