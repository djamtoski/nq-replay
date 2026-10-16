from datetime import date

import pandas as pd
import pytest

import nq_data as nd
from nq_replay import Order, OType, Side


def frame(start_utc: str, n: int, iid=1, base=20000.0):
    idx = pd.date_range(start_utc, periods=n, freq="s", tz="UTC", name="ts_event")
    px = [base + i * 0.25 for i in range(n)]
    return pd.DataFrame(
        {
            "open": px,
            "high": [p + 0.5 for p in px],
            "low": [p - 0.5 for p in px],
            "close": [p + 0.25 for p in px],
            "volume": [3] * n,
            "instrument_id": [iid] * n,
        },
        index=idx,
    )


def test_trading_day_window_est_and_edt():
    s, e = nd.trading_day_window_utc(date(2025, 3, 5))  # EST: 18:00 NY = 23:00 UTC
    assert (s.hour, e.hour, s.day) == (23, 22, 4)
    s, e = nd.trading_day_window_utc(date(2025, 3, 11))  # EDT: 18:00 NY = 22:00 UTC
    assert (s.hour, e.hour) == (22, 21)


def test_monday_window_starts_sunday_evening():
    s, _ = nd.trading_day_window_utc(date(2025, 3, 3))
    assert s.weekday() == 6  # Sunday


def test_trading_days_skip_weekends():
    assert [d.day for d in nd.trading_days(date(2025, 3, 7), date(2025, 3, 11))] == [7, 10, 11]


def test_write_and_load_roundtrip(tmp_path):
    df = frame("2025-03-10 13:30:00", 100)
    nd.write_day(nd.frame_to_table(df), nd.day_path(tmp_path, "NQ.v.0", date(2025, 3, 10)))
    bars = nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10), tmp_path)
    assert len(bars) == 100
    assert bars[0].ts == int(df.index[0].value)
    assert (bars[0].o, bars[0].h, bars[0].l, bars[0].c) == (20000.0, 20000.5, 19999.5, 20000.25)
    assert all(b.dur_ns == 10**9 for b in bars)
    assert all(bars[i].ts < bars[i + 1].ts for i in range(99))


def test_unsorted_input_is_sorted(tmp_path):
    df = frame("2025-03-10 13:30:00", 10).iloc[::-1]
    nd.write_day(nd.frame_to_table(df), nd.day_path(tmp_path, "NQ.v.0", date(2025, 3, 10)))
    ts = [b.ts for b in nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10), tmp_path)]
    assert ts == sorted(ts)


def test_multi_day_concat_in_order(tmp_path):
    for d, start in (
        (date(2025, 3, 11), "2025-03-11 13:30:00"),
        (date(2025, 3, 10), "2025-03-10 13:30:00"),
    ):
        nd.write_day(nd.frame_to_table(frame(start, 5)), nd.day_path(tmp_path, "NQ.v.0", d))
    bars = nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 11), tmp_path)
    assert len(bars) == 10 and bars[4].ts < bars[5].ts


def test_missing_data_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10), tmp_path)


def test_back_adjust_at_roll(tmp_path):
    old = frame("2025-03-10 13:30:00", 5, iid=1, base=20000.0)  # last close 20001.25
    new = frame("2025-03-10 13:30:05", 5, iid=2, base=20050.0)  # first open 20050 (+48.75 gap)
    nd.write_day(
        nd.frame_to_table(pd.concat([old, new])), nd.day_path(tmp_path, "NQ.v.0", date(2025, 3, 10))
    )
    raw = nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10), tmp_path)
    adj = nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10), tmp_path, back_adjust=True)
    assert adj[5].o == raw[5].o  # newest contract untouched
    assert adj[4].c == adj[5].o  # roll gap closed
    assert adj[0].o == raw[0].o + 48.75
    assert all(a.h >= a.l for a in adj)


def test_loaded_bars_drive_replay_end_to_end(tmp_path):
    nd.write_day(
        nd.frame_to_table(frame("2025-03-10 13:30:00", 600)),
        nd.day_path(tmp_path, "NQ.v.0", date(2025, 3, 10)),
    )
    s = nd.load_session("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10), tmp_path, tfs=(60,))
    s.step(5)
    s.broker.submit(s.now, Order(Side.LONG, 1, OType.MARKET, tp=20010))
    s.step(595)
    assert s.cursor == 600 and len(s.mtf.closed(60)) == 9 and s.broker.trades
    assert s.broker.trades[0].reason == "tp"


def test_write_is_atomic_no_tmp_left(tmp_path):
    p = nd.day_path(tmp_path, "NQ.v.0", date(2025, 3, 10))
    nd.write_day(nd.frame_to_table(frame("2025-03-10 13:30:00", 3)), p)
    assert p.exists() and not list(p.parent.glob("*.tmp"))


def test_ingest_requires_key(monkeypatch):
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        nd.cost("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10))
