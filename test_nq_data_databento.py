"""
Offline tests for the Databento path of nq_data.py.

We cannot call Databento here (it needs a paid key), so a FakeHistorical serves a synthetic exchange dataset
as *genuine DBN bytes decoded by the real databento library*: the code under test sees the same DBNStore /
to_df() output (dtypes, UTC index, column names) it will see from the live API. The fake honours the requested
[start, end) window like the real service, so the trading-day window maths is tested against real filtering.
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import databento_dbn as dbn
import pandas as pd
import pyarrow.parquet as pq
import pytest

import nq_data as nd

NY = ZoneInfo("America/New_York")
S = 10**9
FIXED = 10**9          # DBN fixed-point price scale
SECRET = "db-SECRETSECRETSECRETSECRET12345"


# ---------------------------------------------------------------- synthetic exchange
def ns(dt: datetime) -> int:
    return int(dt.timestamp()) * S


def make_dataset(first: date, last: date, step_min: int = 15, roll_after: datetime | None = None) -> list[dict]:
    """
    Sparse 1s bars across [first, last] CME sessions: one every `step_min` minutes plus bars exactly on the
    session edges (17:00:00 excluded, 17:59:59 in the halt, 18:00:00 / 18:00:01 starting the next session).
    Roll: before `roll_after` instrument 100 at ~20000, after it instrument 200 at +50 points.
    """
    out, px = [], 20000.0
    d = first - timedelta(days=1)
    while d <= last:
        # each calendar evening 18:00 NY -> 17:00 NY next day (weekend gap handled by skipping Fri/Sat evenings)
        start = datetime.combine(d, time(18), tzinfo=NY)
        if start.weekday() not in (4, 5):           # no Friday/Saturday evening session
            t = start
            end = datetime.combine(d + timedelta(days=1), time(17), tzinfo=NY)
            stamps = [start, start + timedelta(seconds=1)]
            while t < end:
                stamps.append(t)
                t += timedelta(minutes=step_min)
            stamps += [end - timedelta(seconds=1)]
            for st in sorted(set(stamps)):
                if st >= end:
                    continue
                px += 0.25
                after = roll_after is not None and st >= roll_after
                out.append({"ts": ns(st), "iid": 200 if after else 100, "px": px + (50.0 if after else 0.0)})
        d += timedelta(days=1)
    return out


def dbn_store(rows: list[dict], start_ns: int, end_ns: int):
    meta = dbn.Metadata(dataset="GLBX.MDP3", start=start_ns, end=end_ns, stype_in=dbn.SType.CONTINUOUS,
                        stype_out=dbn.SType.INSTRUMENT_ID, schema=dbn.Schema.OHLCV_1S, symbols=["NQ.v.0"],
                        partial=[], not_found=[], mappings=[])
    msgs = []
    for r in rows:
        p = int(round(r["px"] * FIXED))
        msgs.append(dbn.OHLCVMsg(dbn.RType.OHLCV_1S, 1, r["iid"], r["ts"], p, p + FIXED // 2, p - FIXED // 2, p + FIXED // 4, r.get("vol", 7)))
    return db.DBNStore.from_bytes(bytes(meta.encode()) + b"".join(bytes(m) for m in msgs))


class FakeHistorical:
    """Stands in for databento.Historical; records every call and filters by the requested window."""

    def __init__(self, rows: list[dict], fail_on_call: int | None = None, empty_days: set[date] = frozenset(), cost: float = 1.23):
        self.rows, self.fail_on_call, self.empty_days, self.cost = rows, fail_on_call, set(empty_days), cost
        self.range_calls: list[dict] = []
        self.cost_calls: list[dict] = []
        self.timeseries = self
        self.metadata = self

    def get_range(self, **kw):
        self.range_calls.append(kw)
        if self.fail_on_call is not None and len(self.range_calls) == self.fail_on_call:
            raise ConnectionError("simulated network failure")
        s, e = (int(pd.Timestamp(kw[k]).value) for k in ("start", "end"))
        rows = [r for r in self.rows if s <= r["ts"] < e]
        d = datetime.fromtimestamp(e / S, tz=timezone.utc).astimezone(NY).date()      # the trading date requested
        if d in self.empty_days:
            rows = []
        return dbn_store(rows, s, e)

    def get_cost(self, **kw):
        self.cost_calls.append(kw)
        return self.cost


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "bars"


def patch_client(monkeypatch, fake):
    monkeypatch.setattr(nd, "_client", lambda: fake)
    return fake


WEEK_A = (date(2025, 3, 3), date(2025, 3, 7))           # before the US DST change (Sun 2025-03-09)
ACROSS_DST = (date(2025, 3, 7), date(2025, 3, 12))      # Fri .. Wed straddling the change


# ---------------------------------------------------------------- conversion of REAL library output
def test_frame_to_table_accepts_real_to_df_output():
    rows = [{"ts": ns(datetime(2025, 3, 10, 13, 30, tzinfo=timezone.utc)) + i * S, "iid": 42, "px": 20000.0 + i * 0.25, "vol": 5 + i} for i in range(4)]
    df = dbn_store(rows, rows[0]["ts"], rows[-1]["ts"] + S).to_df(price_type="float", pretty_ts=True)
    assert str(df["volume"].dtype) == "uint64" and str(df["instrument_id"].dtype) == "uint32"      # what the real API yields
    t = nd.frame_to_table(df)
    assert t.schema == nd.SCHEMA
    assert t.column("ts").to_pylist() == [r["ts"] for r in rows]
    assert t.column("o").to_pylist() == [r["px"] for r in rows]
    assert t.column("v").to_pylist() == [5, 6, 7, 8]


def test_large_instrument_ids_do_not_overflow():
    big = 3_000_000_000                                  # uint32 ids above 2**31 are legal
    rows = [{"ts": ns(datetime(2025, 3, 10, 13, 30, tzinfo=timezone.utc)), "iid": big, "px": 20000.0}]
    df = dbn_store(rows, rows[0]["ts"], rows[0]["ts"] + S).to_df(price_type="float", pretty_ts=True)
    assert nd.frame_to_table(df).column("instrument_id").to_pylist() == [big]


# ---------------------------------------------------------------- how we call the API
def test_get_range_is_called_with_the_exact_trading_day_window(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A)))
    nd.ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root)
    (kw,) = fake.range_calls
    assert kw["dataset"] == "GLBX.MDP3" and kw["schema"] == "ohlcv-1s"
    assert kw["stype_in"] == "continuous" and kw["symbols"] == ["NQ.v.0"]
    # Tue 2025-03-04 trading day = Mon 18:00 EST -> Tue 17:00 EST = 23:00Z -> 22:00Z
    assert pd.Timestamp(kw["start"]) == pd.Timestamp("2025-03-03 23:00:00+00:00")
    assert pd.Timestamp(kw["end"]) == pd.Timestamp("2025-03-04 22:00:00+00:00")


def test_cost_spans_first_window_start_to_last_window_end(monkeypatch):
    fake = patch_client(monkeypatch, FakeHistorical([], cost=4.56))
    assert nd.cost("NQ.v.0", date(2025, 3, 10), date(2025, 3, 12)) == 4.56
    (kw,) = fake.cost_calls
    assert pd.Timestamp(kw["start"]) == pd.Timestamp("2025-03-09 22:00:00+00:00")     # Sun 18:00 EDT (after DST began)
    assert pd.Timestamp(kw["end"]) == pd.Timestamp("2025-03-12 21:00:00+00:00")       # Wed 17:00 EDT
    assert (kw["dataset"], kw["schema"], kw["stype_in"], kw["symbols"]) == ("GLBX.MDP3", "ohlcv-1s", "continuous", ["NQ.v.0"])


def test_client_is_built_from_the_environment_key_and_never_printed(monkeypatch, capsys):
    seen = {}

    class Spy(FakeHistorical):
        def __init__(self, key):
            super().__init__([])
            seen["key"] = key

    monkeypatch.setattr(db, "Historical", Spy)
    monkeypatch.setenv("DATABENTO_API_KEY", SECRET)
    nd.cost("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10))
    assert seen["key"] == SECRET
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


# ---------------------------------------------------------------- end-to-end ingest correctness
def all_rows_in_files(root):
    return sorted(r for f in root.glob("NQ.v.0/1s/*/*.parquet") for r in pq.read_table(f).column("ts").to_pylist())


def test_every_bar_lands_in_exactly_one_day_file_across_the_dst_change(monkeypatch, root):
    rows = make_dataset(*ACROSS_DST)
    patch_client(monkeypatch, FakeHistorical(rows))
    n = nd.ingest("NQ.v.0", *ACROSS_DST, root)
    assert n == 4                                         # Fri 7, Mon 10, Tue 11, Wed 12 (weekend skipped)
    got = all_rows_in_files(root)
    in_halt = lambda r: time(17) <= datetime.fromtimestamp(r / S, tz=timezone.utc).astimezone(NY).time() < time(18)
    expected = sorted(r["ts"] for r in rows if not in_halt(r["ts"]) and r["ts"] >= ns(datetime(2025, 3, 6, 18, tzinfo=NY)))
    expected = [t for t in expected if t < ns(datetime(2025, 3, 12, 17, tzinfo=NY))]
    assert got == expected                                # nothing lost, nothing duplicated
    assert len(got) == len(set(got))


def test_each_file_holds_only_its_own_trading_day(monkeypatch, root):
    patch_client(monkeypatch, FakeHistorical(make_dataset(*ACROSS_DST)))
    nd.ingest("NQ.v.0", *ACROSS_DST, root)
    for d in (date(2025, 3, 7), date(2025, 3, 10), date(2025, 3, 11), date(2025, 3, 12)):
        ts = pq.read_table(nd.day_path(root, "NQ.v.0", d)).column("ts").to_pylist()
        lo, hi = nd.trading_day_window_utc(d)
        assert ts and all(ns(lo) <= t < ns(hi) for t in ts), d
        assert ts == sorted(ts)


def test_monday_file_includes_the_sunday_evening_session_after_dst(monkeypatch, root):
    patch_client(monkeypatch, FakeHistorical(make_dataset(*ACROSS_DST)))
    nd.ingest("NQ.v.0", date(2025, 3, 10), date(2025, 3, 10), root)
    ts = pq.read_table(nd.day_path(root, "NQ.v.0", date(2025, 3, 10))).column("ts").to_pylist()
    first = datetime.fromtimestamp(ts[0] / S, tz=timezone.utc)
    assert first == datetime(2025, 3, 9, 22, 0, tzinfo=timezone.utc)      # Sun 18:00 EDT = 22:00Z (it was 23:00Z the week before)
    assert any(t == ns(datetime(2025, 3, 9, 22, 0, 1, tzinfo=timezone.utc)) for t in ts)   # the 18:00:01 bar is kept too


def test_roundtrip_load_matches_source_and_back_adjusts_across_a_roll_at_a_day_boundary(monkeypatch, root):
    roll = datetime(2025, 3, 11, 18, 0, tzinfo=NY)        # new contract starts with Wednesday's session
    rows = make_dataset(date(2025, 3, 10), date(2025, 3, 12), roll_after=roll)
    patch_client(monkeypatch, FakeHistorical(rows))
    nd.ingest("NQ.v.0", date(2025, 3, 10), date(2025, 3, 12), root)
    bars = nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 12), root)
    assert [b.ts for b in bars] == sorted(b.ts for b in bars)
    src = {r["ts"]: r for r in rows}
    assert all(abs(b.o - src[b.ts]["px"]) < 1e-9 for b in bars)            # exact prices survive DBN -> Parquet -> Bar
    adj = nd.load_bars("NQ.v.0", date(2025, 3, 10), date(2025, 3, 12), root, back_adjust=True)
    rolled = next(i for i in range(1, len(bars)) if src[bars[i].ts]["iid"] != src[bars[i - 1].ts]["iid"])
    assert adj[rolled].o == bars[rolled].o                                 # newest contract untouched
    assert abs(adj[rolled].o - adj[rolled - 1].c) < 0.25                   # roll gap closed to within a tick
    assert adj[0].o == pytest.approx(bars[0].o + (bars[rolled].o - bars[rolled - 1].c), abs=0.25)


# ---------------------------------------------------------------- resumability and failure handling
def test_second_run_skips_existing_days_and_force_redownloads(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A)))
    assert nd.ingest("NQ.v.0", *WEEK_A, root) == 5
    calls = len(fake.range_calls)
    assert nd.ingest("NQ.v.0", *WEEK_A, root) == 0
    assert len(fake.range_calls) == calls                                  # no API calls (= no cost) for days we have
    assert nd.ingest("NQ.v.0", date(2025, 3, 5), date(2025, 3, 5), root, force=True) == 1
    assert len(fake.range_calls) == calls + 1


def test_holiday_with_no_data_writes_nothing_and_does_not_crash(monkeypatch, root, capsys):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), empty_days={date(2025, 3, 5)}))
    n = nd.ingest("NQ.v.0", *WEEK_A, root)
    assert n == 4
    assert not nd.day_path(root, "NQ.v.0", date(2025, 3, 5)).exists()
    assert "no data" in capsys.readouterr().out


def test_failure_midway_keeps_finished_days_leaves_no_partial_files_and_resumes(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), fail_on_call=3))
    with pytest.raises(ConnectionError):
        nd.ingest("NQ.v.0", *WEEK_A, root)
    files = sorted(p.name for p in root.glob("NQ.v.0/1s/*/*"))
    assert files == ["2025-03-03.parquet", "2025-03-04.parquet"]           # the two completed days only, no *.tmp
    fake.fail_on_call = None
    fake.range_calls.clear()
    assert nd.ingest("NQ.v.0", *WEEK_A, root) == 3                         # resumes exactly the missing days
    assert len(fake.range_calls) == 3
    assert len(list(root.glob("NQ.v.0/1s/*/*.parquet"))) == 5


def test_weekend_only_range_makes_no_calls(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical([]))
    assert nd.ingest("NQ.v.0", date(2025, 3, 8), date(2025, 3, 9), root) == 0
    assert fake.range_calls == []


# ---------------------------------------------------------------- CLI
def run_cli(argv, monkeypatch, root, answers=()):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            nd.main(argv + ["--root", str(root)])
        except SystemExit as e:
            return buf.getvalue() + f"[exit {e.code}]"
    return buf.getvalue()


def test_cli_cost_prints_the_estimate_and_downloads_nothing(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=7.5))
    out = run_cli(["cost", "--start", "2025-03-03", "--end", "2025-03-07"], monkeypatch, root)
    assert "$7.50" in out and fake.range_calls == []


def test_cli_ingest_asks_first_and_aborts_on_no(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=2.0))
    out = run_cli(["ingest", "--start", "2025-03-03", "--end", "2025-03-07"], monkeypatch, root, answers=["n"])
    assert "aborted" in out and fake.range_calls == [] and not list(root.glob("**/*.parquet"))


def test_cli_ingest_with_yes_flag_skips_the_prompt_and_downloads(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=2.0))
    out = run_cli(["ingest", "--start", "2025-03-03", "--end", "2025-03-04", "--yes"], monkeypatch, root)
    assert "done: 2 day(s) written" in out and len(fake.range_calls) == 2


def test_cli_ingest_confirms_with_y(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=2.0))
    out = run_cli(["ingest", "--start", "2025-03-03", "--end", "2025-03-03"], monkeypatch, root, answers=["Y"])
    assert "done: 1 day(s) written" in out


def test_cli_requires_dates_and_lists_files(monkeypatch, root):
    patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A)))
    assert "[exit 2]" in run_cli(["cost"], monkeypatch, root)
    run_cli(["ingest", "--start", "2025-03-03", "--end", "2025-03-03", "--yes"], monkeypatch, root)
    out = run_cli(["info"], monkeypatch, root)
    assert "2025-03-03.parquet" in out and "rows=" in out


def test_cli_without_a_key_explains_instead_of_crashing(monkeypatch, root):
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    out = run_cli(["cost", "--start", "2025-03-03", "--end", "2025-03-03"], monkeypatch, root)
    assert "DATABENTO_API_KEY is not set" in out


def test_cli_turns_api_rejections_into_a_clear_message_and_keeps_finished_days(monkeypatch, root):
    class Rejecting(FakeHistorical):
        def get_range(self, **kw):
            if len(self.range_calls) == 2:               # third day: e.g. date beyond what the service has yet
                self.range_calls.append(kw)
                raise db.BentoClientError(422, message="data_end_after_available_end: end is after the available data")
            return super().get_range(**kw)

    patch_client(monkeypatch, Rejecting(make_dataset(*WEEK_A)))
    out = run_cli(["ingest", "--start", "2025-03-03", "--end", "2025-03-07", "--yes"], monkeypatch, root)
    assert "Databento rejected the request" in out and "data_end_after_available_end" in out
    assert "Traceback" not in out and "re-run to resume" in out
    assert len(list(root.glob("NQ.v.0/1s/*/*.parquet"))) == 2        # first two days are kept
    assert not list(root.glob("NQ.v.0/1s/*/*.tmp"))


def test_cli_also_handles_a_rejection_at_the_cost_step(monkeypatch, root):
    class BadKey(FakeHistorical):
        def get_cost(self, **kw):
            raise db.BentoClientError(401, message="auth_authentication_failed: invalid API key")

    patch_client(monkeypatch, BadKey([]))
    out = run_cli(["cost", "--start", "2025-03-03", "--end", "2025-03-03"], monkeypatch, root)
    assert "Databento rejected the request" in out and "invalid API key" in out and "Traceback" not in out


# ---------------------------------------------------------------- review fixes
def pin_now(monkeypatch, when):
    monkeypatch.setattr(nd, "_utcnow", lambda: when)


MAR4_CLOSE = datetime(2025, 3, 4, 22, 0, tzinfo=timezone.utc)           # Tue 17:00 EST


def test_a_trading_day_that_has_not_finished_is_never_stored(monkeypatch, root, capsys):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A)))
    for now in (MAR4_CLOSE - timedelta(hours=2), MAR4_CLOSE + timedelta(hours=1, minutes=59)):     # open / not yet settled
        pin_now(monkeypatch, now)
        assert nd.ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root) == 0
    assert fake.range_calls == [] and not list(root.glob("**/*.parquet"))
    assert "not finished yet" in capsys.readouterr().out
    plan = nd.plan_ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root)
    assert plan.todo == [] and plan.unsettled == [date(2025, 3, 4)]
    pin_now(monkeypatch, MAR4_CLOSE + SETTLE_PLUS)                                                  # settled: now it downloads
    assert nd.ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root) == 1


SETTLE_PLUS = nd.SETTLE + timedelta(seconds=1)


def test_unfinished_days_are_left_out_of_the_cost_and_reported_by_the_cli(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical([], cost=3.0))
    pin_now(monkeypatch, MAR4_CLOSE + timedelta(hours=1))                   # Mar 3 settled, Mar 4 not
    out = run_cli(["cost", "--start", "2025-03-03", "--end", "2025-03-04"], monkeypatch, root)
    assert "1 trading day(s) to download, 0 already on disk, 1 not finished yet" in out
    assert len(fake.cost_calls) == 1
    assert pd.Timestamp(fake.cost_calls[0]["end"]) == pd.Timestamp("2025-03-03 22:00:00+00:00")   # Mar 3's close only


def test_different_symbols_never_share_files(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A)))
    assert nd.ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root) == 1
    assert nd.ingest("NQ.c.0", date(2025, 3, 4), date(2025, 3, 4), root) == 1      # not skipped as "already present"
    assert [c["symbols"] for c in fake.range_calls] == [["NQ.v.0"], ["NQ.c.0"]]
    assert nd.day_path(root, "NQ.v.0", date(2025, 3, 4)).exists() and nd.day_path(root, "NQ.c.0", date(2025, 3, 4)).exists()
    assert nd.day_path(root, "NQ.v.0", date(2025, 3, 4)) != nd.day_path(root, "NQ.c.0", date(2025, 3, 4))
    assert len(nd.load_bars("NQ.c.0", date(2025, 3, 4), date(2025, 3, 4), root)) > 0


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "NQ..v.0", "x" * 40, "-NQ", "a b", "C:\\x", "NQ/../../etc", ".hidden"])
def test_symbols_that_could_escape_the_data_root_are_rejected(bad, monkeypatch, root):
    patch_client(monkeypatch, FakeHistorical([]))
    for call in (lambda: nd.day_path(root, bad, date(2025, 3, 4)),
                 lambda: nd.ingest(bad, date(2025, 3, 4), date(2025, 3, 4), root),
                 lambda: nd.load_bars(bad, date(2025, 3, 4), date(2025, 3, 4), root)):
        with pytest.raises(ValueError, match="invalid symbol"):
            call()
    out = run_cli(["cost", f"--symbol={bad}", "--start", "2025-03-04", "--end", "2025-03-04"], monkeypatch, root)
    assert "[exit 2]" in out and "invalid symbol" in out


def test_cost_covers_only_the_days_that_would_be_downloaded(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=1.5))
    nd.ingest("NQ.v.0", date(2025, 3, 3), date(2025, 3, 5), root)
    fake.cost_calls.clear()
    assert nd.cost("NQ.v.0", *WEEK_A, root) == 1.5                                   # only Thu 6 + Fri 7 remain: one window
    (kw,) = fake.cost_calls
    assert pd.Timestamp(kw["start"]) == pd.Timestamp("2025-03-05 23:00:00+00:00")   # Thu 6's session starts Wed 18:00 EST
    assert pd.Timestamp(kw["end"]) == pd.Timestamp("2025-03-07 22:00:00+00:00")
    nd.ingest("NQ.v.0", *WEEK_A, root)
    fake.cost_calls.clear()
    assert nd.cost("NQ.v.0", *WEEK_A, root) == 0.0 and fake.cost_calls == []        # nothing left: nothing priced


def test_cost_prices_separate_windows_for_separated_missing_days(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=1.5))
    nd.ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root)                    # Tue exists; Mon and Wed..Fri missing
    fake.cost_calls.clear()
    assert nd.cost("NQ.v.0", *WEEK_A, root) == 3.0                                   # Mon | Wed-Fri = two requests
    assert len(fake.cost_calls) == 2


def test_friday_and_monday_are_one_contiguous_window(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical([], cost=2.0))
    nd.cost("NQ.v.0", date(2025, 3, 7), date(2025, 3, 10), root)
    assert len(fake.cost_calls) == 1                                                 # no pointless split at the weekend


def test_force_prices_and_downloads_everything_again(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=1.0))
    nd.ingest("NQ.v.0", *WEEK_A, root)
    fake.cost_calls.clear()
    assert nd.cost("NQ.v.0", *WEEK_A, root, force=True) == 1.0 and len(fake.cost_calls) == 1


def test_cli_reports_how_much_is_left_and_skips_the_prompt_when_nothing_is(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=2.0))
    nd.ingest("NQ.v.0", date(2025, 3, 3), date(2025, 3, 4), root)
    out = run_cli(["cost", "--start", "2025-03-03", "--end", "2025-03-05"], monkeypatch, root)
    assert "1 trading day(s) to download, 2 already on disk" in out
    fake.cost_calls.clear(); fake.range_calls.clear()
    nd.ingest("NQ.v.0", *WEEK_A, root)
    out = run_cli(["ingest", "--start", "2025-03-03", "--end", "2025-03-07"], monkeypatch, root)   # no answers supplied: no prompt
    assert "nothing to download" in out and fake.cost_calls == []


def test_yes_has_a_default_spending_ceiling_and_max_cost_overrides_it(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=100.0))
    args = ["ingest", "--start", "2025-03-03", "--end", "2025-03-03"]
    out = run_cli(args + ["--yes"], monkeypatch, root)
    assert "above the $25.00 limit" in out and "--max-cost" in out and fake.range_calls == []
    out = run_cli(args + ["--yes", "--max-cost", "50"], monkeypatch, root)
    assert "above the $50.00 limit" in out and fake.range_calls == []
    out = run_cli(args + ["--yes", "--max-cost", "150"], monkeypatch, root)
    assert "done: 1 day(s) written" in out


def test_interactive_runs_have_no_default_ceiling_but_honour_an_explicit_one(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=100.0))
    args = ["ingest", "--start", "2025-03-03", "--end", "2025-03-03"]
    assert "Estimated Databento cost: $100.00" in run_cli(args, monkeypatch, root, answers=["n"])      # shown, user decides
    out = run_cli(args + ["--max-cost", "10"], monkeypatch, root, answers=["y"])
    assert "above the $10.00 limit" in out and fake.range_calls == []
    assert "done: 1 day(s) written" in run_cli(args, monkeypatch, root, answers=["y"])


def test_a_non_interactive_run_without_yes_aborts_instead_of_crashing(monkeypatch, root):
    fake = patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A), cost=2.0))
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(EOFError()))
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        nd.main(["ingest", "--start", "2025-03-03", "--end", "2025-03-03", "--root", str(root)])
    assert "aborted" in buf.getvalue() and "Traceback" not in buf.getvalue() and fake.range_calls == []


@pytest.mark.parametrize("where, offset_s", [("exactly at the exclusive end", 0), ("a few seconds after the end", 5), ("one second before the start", -1)])
def test_bars_outside_the_requested_window_are_rejected_and_nothing_is_written(monkeypatch, root, where, offset_s):
    class Leaky(FakeHistorical):
        def get_range(self, **kw):
            self.range_calls.append(kw)
            s, e = (int(pd.Timestamp(kw[k]).value) for k in ("start", "end"))
            stray = (s if offset_s < 0 else e) + offset_s * S                 # one stray bar next to the window edge
            rows = [r for r in self.rows if s <= r["ts"] < e] + [{"ts": stray, "iid": 100, "px": 20001.0}]
            return dbn_store(rows, s, e)

    patch_client(monkeypatch, Leaky(make_dataset(*WEEK_A)))
    with pytest.raises(ValueError, match="outside the requested window"):
        nd.ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root)
    assert not list(root.glob("**/*.parquet")) and not list(root.glob("**/*.tmp"))


def test_bars_exactly_on_the_inclusive_start_and_just_before_the_end_are_accepted(monkeypatch, root):
    rows = make_dataset(*WEEK_A)
    s, e = nd.trading_day_window_utc(date(2025, 3, 4))
    edge = [r for r in rows if ns(s) <= r["ts"] < ns(e)]
    assert edge[0]["ts"] == ns(s) and edge[-1]["ts"] == ns(e) - S                 # the dataset really includes both edges
    patch_client(monkeypatch, FakeHistorical(rows))
    assert nd.ingest("NQ.v.0", date(2025, 3, 4), date(2025, 3, 4), root) == 1


def test_temp_files_are_unique_per_write_and_removed_on_failure(monkeypatch, tmp_path):
    tbl = nd.frame_to_table(dbn_store([{"ts": ns(datetime(2025, 3, 10, 13, 30, tzinfo=timezone.utc)), "iid": 1, "px": 1.0}],
                                      0, 1).to_df(price_type="float", pretty_ts=True))
    target = tmp_path / "d" / "2025-03-10.parquet"
    seen = []
    real = pq.write_table

    def spy(table, where, **kw):
        seen.append(Path(where).name)
        return real(table, where, **kw)

    monkeypatch.setattr(nd.pq, "write_table", spy)
    nd.write_day(tbl, target); nd.write_day(tbl, target)
    assert len(set(seen)) == 2 and all(n.endswith(".tmp") and n.startswith("2025-03-10.") for n in seen)   # distinct per write
    assert [p.name for p in target.parent.iterdir()] == ["2025-03-10.parquet"]

    def boom(table, where, **kw):
        real(table, where, **kw)
        raise OSError("disk full after writing")

    monkeypatch.setattr(nd.pq, "write_table", boom)
    other = tmp_path / "d" / "2025-03-11.parquet"
    with pytest.raises(OSError):
        nd.write_day(tbl, other)
    assert not other.exists() and not list(target.parent.glob("*.tmp"))                    # the half-written temp is cleaned up


def test_api_key_never_appears_in_any_cli_output(monkeypatch, root):
    monkeypatch.setenv("DATABENTO_API_KEY", SECRET)
    patch_client(monkeypatch, FakeHistorical(make_dataset(*WEEK_A)))
    out = run_cli(["ingest", "--start", "2025-03-03", "--end", "2025-03-04", "--yes"], monkeypatch, root)
    out += run_cli(["cost", "--start", "2025-03-03", "--end", "2025-03-04"], monkeypatch, root)
    assert SECRET not in out
