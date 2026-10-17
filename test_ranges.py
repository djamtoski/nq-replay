"""The replay starts on the first day of the chosen range; the datasets list tells the UI what exists."""

from datetime import UTC, date, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

import nq_data as nd
import server

NY = server.NY


@pytest.fixture()
def client():
    server.RUNTIMES.clear()
    with TestClient(server.app) as c:
        yield c
    server.RUNTIMES.clear()


def make(client, **kw):
    r = client.post("/sessions", json={"source": "synthetic", "latency_ms": 0, **kw})
    return r


def first_revealed_ny(sid):
    ss = server.RUNTIMES[sid].ss
    return datetime.fromtimestamp(ss.bars[ss.cursor].ts / 1e9, tz=UTC).astimezone(NY)


def test_synthetic_starts_on_the_first_day_of_the_range_at_the_chosen_time(client):
    r = make(client, start="2025-03-04", end="2025-03-05", start_time="09:30")
    assert r.status_code == 200, r.text
    start = first_revealed_ny(r.json()["id"])
    assert (start.date(), start.hour, start.minute) == (date(2025, 3, 4), 9, 30)


def test_the_whole_range_is_replayable_not_just_the_last_eight_hours(client):
    r = make(client, start="2025-03-04", end="2025-03-06", warmup_days=1)
    sid = r.json()["id"]
    ss = server.RUNTIMES[sid].ss
    left = (ss.bars[-1].ts - ss.bars[ss.cursor].ts) / 1e9 / 3600
    assert left > 2 * 23  # roughly three trading days of bars remain after the start
    last = datetime.fromtimestamp(ss.bars[-1].ts / 1e9, tz=UTC).astimezone(NY)
    assert last.date() == date(2025, 3, 6) and last.hour == 16


def test_chart_history_comes_from_the_trading_days_before_the_start(client):
    sid = make(client, start="2025-03-10", end="2025-03-10", warmup_days=2).json()["id"]
    ss = server.RUNTIMES[sid].ss
    first = datetime.fromtimestamp(ss.bars[0].ts / 1e9, tz=UTC).astimezone(NY)
    assert first.date() == date(
        2025, 3, 5
    )  # Wed 18:00 NY opens Thursday's session, 2 trading days back
    assert ss.cursor > 2 * 80_000  # history was preloaded, not left to play


def test_default_range_is_the_five_day_demo_week(client):
    sid = make(client).json()["id"]
    assert first_revealed_ny(sid).date() == server.SYNTH_FIRST


def test_synthetic_range_limits(client):
    r = make(client, start="2025-03-05", end="2025-03-04")
    assert r.status_code == 422 and r.json()["detail"] == "The end date is before the start date"
    r = make(client, start="2025-03-03", end="2025-03-28")
    assert r.status_code == 422 and "limited to 10 trading days" in r.json()["detail"]
    assert make(client, start="2025-03-03", end="2025-03-14").status_code == 200  # exactly 10 days


def test_back_steps_over_weekends():
    assert server._back(date(2025, 3, 10), 1) == date(2025, 3, 7)  # Monday -> Friday
    assert server._back(date(2025, 3, 10), 3) == date(2025, 3, 5)
    assert server._back(date(2025, 3, 10), 0) == date(2025, 3, 10)


# ---------------------------------------------------------------- datasets
def write_day(root, symbol, d):
    t = pa.table(
        {
            "ts": pa.array([1], pa.int64()),
            "o": [1.0],
            "h": [1.0],
            "l": [1.0],
            "c": [1.0],
            "v": pa.array([1], pa.int64()),
            "instrument_id": pa.array([0], pa.uint32()),
        },
        schema=nd.SCHEMA,
    )
    nd.write_day(t, nd.day_path(root, symbol, d))


def test_list_datasets_reports_range_and_day_count(tmp_path):
    for i in range(3):
        write_day(tmp_path, "USATECH.IDX", date(2025, 3, 3) + timedelta(days=i))
    write_day(tmp_path, "NQ.v.0", date(2025, 6, 2))
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "1s").mkdir()
    (tmp_path / "USATECH.IDX" / "1s" / "2025" / "notes.parquet").write_bytes(
        b"x"
    )  # not a date: ignored
    got = {d["symbol"]: d for d in nd.list_datasets(tmp_path)}
    assert got["USATECH.IDX"] == {
        "symbol": "USATECH.IDX",
        "first": "2025-03-03",
        "last": "2025-03-05",
        "days": 3,
    }
    assert got["NQ.v.0"]["days"] == 1 and "junk" not in got
    assert nd.list_datasets(tmp_path / "missing") == []


def test_datasets_endpoint_labels_sources(client, tmp_path, monkeypatch):
    monkeypatch.setattr(nd, "DEFAULT_ROOT", tmp_path)
    assert client.get("/datasets").json() == {"datasets": []}
    write_day(tmp_path, "USATECH.IDX", date(2025, 3, 3))
    write_day(tmp_path, "NQ.v.0", date(2025, 3, 3))
    labels = {d["symbol"]: d["label"] for d in client.get("/datasets").json()["datasets"]}
    assert labels == {
        "NQ.v.0": "NQ.v.0 (Databento)",
        "USATECH.IDX": "Nasdaq 100 CFD (Dukascopy)",
    }


def test_a_downloaded_dataset_can_be_replayed_from_its_first_day(client, tmp_path, monkeypatch):
    """End to end: Parquet day files -> /sessions -> the cursor sits on the first day at 09:00 NY."""
    import dukascopy as dk
    from test_dukascopy import FakeFeed, bi5, hour

    monkeypatch.setattr(nd, "DEFAULT_ROOT", tmp_path)
    # 09:00-09:01 NY on Tue 2025-03-11 is 13:00Z (EDT)
    quotes = [(i * 1000, 100.0 + i, 100.0 + i) for i in range(60)]
    feed = FakeFeed({hour(2025, 3, 11, 13): bi5(quotes), hour(2025, 3, 11, 12): bi5(quotes)})
    dk.ingest(date(2025, 3, 11), date(2025, 3, 11), tmp_path, fetch=feed, say=lambda _m: None)
    assert pq.read_table(nd.day_path(tmp_path, dk.SYMBOL, date(2025, 3, 11))).num_rows == 120
    r = client.post(
        "/sessions",
        json={
            "source": "parquet",
            "symbol": dk.SYMBOL,
            "start": "2025-03-11",
            "end": "2025-03-11",
            "start_time": "09:00",
            "warmup_days": 0,
        },
    )
    assert r.status_code == 200, r.text
    start = first_revealed_ny(r.json()["id"])
    assert (start.date(), start.hour, start.minute) == (date(2025, 3, 11), 9, 0)
