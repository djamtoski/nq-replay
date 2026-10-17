"""Yahoo NQ=F 1-minute import (offline, hand-built chart responses) and the replay on 1-minute candles."""

import json
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

import nq_data as nd
import server
import yahoo as yh
from nq_replay import Order, OType, Side


def utc(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


def chart(rows):
    """rows: [(epoch s, o, h, l, c, v)] -> a Yahoo v8 chart response."""
    ts = [r[0] for r in rows]
    q = {
        k: [r[i + 1] for r in rows]
        for i, k in enumerate(("open", "high", "low", "close", "volume"))
    }
    return json.dumps(
        {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}], "error": None}}
    ).encode()


def candle(t, px):
    return (t, px, px + 2, px - 1, px + 1, 100)


# Mon 2025-03-10 is cut off at the start; Tue 03-11 is complete; Wed 03-12 is still open.
ROWS = (
    [candle(utc(2025, 3, 10, 2, i), 100 + i) for i in range(3)]  # Monday, starts part-way through
    + [
        candle(utc(2025, 3, 10, 22, 0), 200),
        candle(utc(2025, 3, 10, 22, 1), 201),
    ]  # Tue 18:00 NY open
    + [candle(utc(2025, 3, 11, 13, 30), 205), candle(utc(2025, 3, 11, 20, 59), 210)]
    + [candle(utc(2025, 3, 11, 22, 0), 300), candle(utc(2025, 3, 12, 10, 0), 301)]  # Wednesday
)
NOW = datetime(2025, 3, 12, 12, 0, tzinfo=UTC)  # Wed midday: Wednesday's session has not closed


@pytest.fixture(autouse=True)
def pinned_now(monkeypatch):
    monkeypatch.setattr(nd, "_utcnow", lambda: NOW)


# ---------------------------------------------------------------- parsing
def test_parse_drops_empty_candles_and_aligns_to_the_minute():
    raw = json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "timestamp": [1000, 1060, 1120],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [1, None, 3],
                                    "high": [2, None, 4],
                                    "low": [0, None, 2],
                                    "close": [1.5, None, 3.5],
                                    "volume": [10, None, None],
                                }
                            ]
                        },
                    }
                ]
            }
        }
    ).encode()
    assert yh.parse(raw) == [(960, 1.0, 2.0, 0.0, 1.5, 10), (1080, 3.0, 4.0, 2.0, 3.5, 0)]


@pytest.mark.parametrize(
    "raw", [b"not json", b"{}", b'{"chart": {"result": null}}', b'{"chart": {"result": []}}']
)
def test_parse_rejects_things_that_are_not_chart_responses(raw):
    with pytest.raises(ValueError, match="not a Yahoo chart response"):
        yh.parse(raw)


def test_candles_are_grouped_by_cme_trading_day():
    days = yh.by_trading_day(yh.parse(chart(ROWS)))
    assert sorted(days) == [date(2025, 3, 10), date(2025, 3, 11), date(2025, 3, 12)]
    assert len(days[date(2025, 3, 11)]) == 4  # 18:00 NY the evening before belongs to Tuesday


# ---------------------------------------------------------------- ingest rules
def test_only_complete_finished_days_are_stored(tmp_path):
    out = []
    n = yh.ingest(chart(ROWS), tmp_path, say=out.append)
    assert n == 1
    assert nd.day_path(tmp_path, yh.SYMBOL, date(2025, 3, 11)).exists()
    assert not nd.day_path(tmp_path, yh.SYMBOL, date(2025, 3, 10)).exists()
    assert not nd.day_path(tmp_path, yh.SYMBOL, date(2025, 3, 12)).exists()
    assert any("part-way" in m for m in out) and any("not finished" in m for m in out)


def test_stored_day_has_minute_duration_and_the_standard_columns(tmp_path):
    yh.ingest(chart(ROWS), tmp_path, say=lambda _m: None)
    t = pq.read_table(nd.day_path(tmp_path, yh.SYMBOL, date(2025, 3, 11)))
    assert t.num_rows == 4 and set(t["dur"].to_pylist()) == {60 * 10**9}
    assert t["ts"].to_pylist()[0] == utc(2025, 3, 10, 22, 0) * 10**9
    assert set(nd.SCHEMA.names) <= set(t.schema.names)


def test_existing_days_are_kept_unless_forced(tmp_path):
    yh.ingest(chart(ROWS), tmp_path, say=lambda _m: None)
    p = nd.day_path(tmp_path, yh.SYMBOL, date(2025, 3, 11))
    before = p.stat().st_mtime_ns
    assert yh.ingest(chart(ROWS), tmp_path, say=lambda _m: None) == 0
    assert p.stat().st_mtime_ns == before
    assert yh.ingest(chart(ROWS), tmp_path, force=True, say=lambda _m: None) == 1


def test_days_accumulate_across_runs(tmp_path, monkeypatch):
    first = [candle(utc(2025, 3, 10, 22, 0), 200), candle(utc(2025, 3, 11, 20, 59), 210)]
    later = [candle(utc(2025, 3, 11, 22, 0), 300), candle(utc(2025, 3, 12, 20, 59), 310)]
    monkeypatch.setattr(nd, "_utcnow", lambda: datetime(2025, 3, 13, 12, 0, tzinfo=UTC))
    yh.ingest(chart(first), tmp_path, say=lambda _m: None)
    yh.ingest(chart(later), tmp_path, say=lambda _m: None)
    assert [d["days"] for d in nd.list_datasets(tmp_path)] == [2]


def test_loader_reads_the_duration_column(tmp_path):
    yh.ingest(chart(ROWS), tmp_path, say=lambda _m: None)
    bars = nd.load_bars(yh.SYMBOL, date(2025, 3, 11), date(2025, 3, 11), tmp_path)
    assert len(bars) == 4 and {b.dur_ns for b in bars} == {60 * 10**9}
    assert bars[0].close_ts == bars[0].ts + 60 * 10**9


def test_one_second_datasets_still_load_with_one_second_bars(tmp_path):
    import dukascopy as dk
    from test_dukascopy import FakeFeed, bi5, hour

    feed = FakeFeed({hour(2025, 3, 10, 22): bi5([(0, 100.0, 100.0)])})
    dk.ingest(date(2025, 3, 11), date(2025, 3, 11), tmp_path, fetch=feed, say=lambda _m: None)
    bars = nd.load_bars(dk.SYMBOL, date(2025, 3, 11), date(2025, 3, 11), tmp_path)
    assert {b.dur_ns for b in bars} == {10**9}


# ---------------------------------------------------------------- the replay on 1-minute candles
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(nd, "DEFAULT_ROOT", tmp_path)
    yh.ingest(chart(ROWS), tmp_path, say=lambda _m: None)
    server.RUNTIMES.clear()
    with TestClient(server.app) as c:
        yield c
    server.RUNTIMES.clear()


def make(client, **kw):
    body = {
        "source": "parquet",
        "symbol": yh.SYMBOL,
        "start": "2025-03-11",
        "end": "2025-03-11",
        "start_time": "09:00",
        "warmup_days": 0,
        "latency_ms": 0,
        **kw,
    }
    return client.post("/sessions", json=body)


def test_timeframes_below_one_minute_are_not_offered(client):
    r = make(client)
    assert r.status_code == 200, r.text
    ss = server.RUNTIMES[r.json()["id"]].ss
    assert ss.tfs == (60, 300, 900, 3600, 14400)
    assert ss.bars[0].dur_ns == 60 * 10**9


def test_a_session_that_only_asks_for_finer_timeframes_is_refused(client):
    r = make(client, tfs=[1, 15])
    assert r.status_code == 422 and "shorter than the data" in r.json()["detail"]


def test_websocket_serves_the_finest_available_timeframe_and_reports_what_exists(client):
    sid = make(client).json()["id"]
    with client.websocket_connect(f"/ws/{sid}?tf=1") as ws:
        r = ws.receive_json()
    assert r["type"] == "reset" and r["tf"] == 60 and r["tfs"] == [60, 300, 900, 3600, 14400]


def test_an_unknown_larger_timeframe_is_still_rejected(client):
    sid = make(client).json()["id"]
    with pytest.raises(Exception):  # noqa: B017 - the server closes the socket with code 4400
        with client.websocket_connect(f"/ws/{sid}?tf=61") as ws:
            ws.receive_json()


def test_candles_close_a_minute_after_they_open_and_trades_fill(client):
    sid = make(client).json()["id"]
    ss = server.RUNTIMES[sid].ss
    ss.step(1)
    assert (
        ss.now == ss.bars[ss.cursor - 1].ts + 60 * 10**9
    )  # a revealed candle is closed 60 s after it opened
    ss.submit(Order(Side.LONG, 1, OType.MARKET))
    ss.step(1)
    assert len(ss.broker.positions) == 1


def test_dataset_is_labelled(client):
    labels = {d["symbol"]: d["label"] for d in client.get("/datasets").json()["datasets"]}
    assert labels[yh.SYMBOL] == "NQ futures (Yahoo, 1-minute)"


def test_a_day_the_response_stops_in_the_middle_of_is_not_stored(tmp_path):
    rows = [
        candle(utc(2025, 3, 10, 22, 0), 200),
        candle(utc(2025, 3, 11, 15, 0), 205),
    ]  # ends 11:00 NY, close is 17:00
    out = []
    assert yh.ingest(chart(rows), tmp_path, say=out.append) == 0
    assert any("stops part-way" in m for m in out)
