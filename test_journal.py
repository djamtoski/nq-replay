import json
import sqlite3
from datetime import UTC, datetime

import pytest

import journal as jr
from nq_replay import ClosedTrade, Side

S = 10**9


def ts(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())


def trade(
    opened,
    closed,
    pnl=100.0,
    side=Side.LONG,
    qty=1,
    entry=100.0,
    exit=105.0,
    r=1.0,
    mae=1.0,
    mfe=6.0,
):
    return ClosedTrade(side, qty, entry, exit, pnl, r, opened * S, closed * S, "tp", mae, mfe)


@pytest.fixture()
def j(tmp_path):
    return jr.Journal(tmp_path / "j.sqlite")


META = {"instrument": "NQ", "point_value": 20.0, "request": {"source": "synthetic"}}
# 2025-03-11 15:00 UTC = 11:00 New York (EDT)
T = ts(2025, 3, 11, 15)


# ---------------- trading day ----------------
@pytest.mark.parametrize(
    "when, day",
    [
        (ts(2025, 3, 11, 15), "2025-03-11"),  # Tuesday morning
        (ts(2025, 3, 11, 22, 30), "2025-03-12"),  # 18:30 NY Tuesday belongs to Wednesday's session
        (ts(2025, 3, 14, 22, 30), "2025-03-17"),  # Friday 18:30 NY -> Monday
        (ts(2025, 3, 9, 23, 0), "2025-03-10"),  # Sunday 19:00 NY (EDT) -> Monday
        (ts(2025, 3, 11, 21, 59), "2025-03-11"),  # 17:59 NY is still the same session
    ],
)
def test_trading_day(when, day):
    assert jr.trading_day(when) == day


# ---------------- notes validation ----------------
def test_clean_notes_accepts_and_normalises():
    out = jr.clean_notes(
        {
            "setup": "  ORB ",
            "emotion": "",
            "rating": 4,
            "mistakes": ["FOMO", "FOMO", " ", "late"],
            "note": "x",
        }
    )
    assert out["setup"] == "ORB" and out["emotion"] is None and out["rating"] == 4
    assert json.loads(out["mistakes"]) == ["FOMO", "late"] and out["note"] == "x"


@pytest.mark.parametrize(
    "bad",
    [
        {"rating": 0},
        {"rating": 6},
        {"rating": True},
        {"rating": "3"},
        {"setup": "x" * 41},
        {"setup": 5},
        {"mistakes": "FOMO"},
        {"mistakes": ["x"] * 11},
        {"mistakes": [1]},
        {"mistakes": ["y" * 41]},
        {"note": "n" * 5001},
        {"note": None},
        {"pnl": 1e9},
    ],
)
def test_clean_notes_rejects(bad):
    with pytest.raises(ValueError):
        jr.clean_notes(bad)


# ---------------- sync ----------------
def test_sync_writes_trades_and_is_idempotent(j):
    ts_ = [trade(T, T + 60), trade(T + 120, T + 180, pnl=-50.0, r=-0.5)]
    assert j.sync("s1", ts_, META) == 2
    assert j.sync("s1", ts_, META) == 0  # nothing new
    rows = j.trades()
    assert len(rows) == 2 and {r["pnl"] for r in rows} == {100.0, -50.0}
    assert rows[0]["day"] == "2025-03-11" and rows[0]["instrument"] == "NQ"
    assert j.session_meta("s1")["request"] == {"source": "synthetic"}


def test_sync_appends_only_new_trades(j):
    a, b, c = trade(T, T + 60), trade(T + 120, T + 180), trade(T + 240, T + 300)
    assert j.sync("s", [a], META) == 1
    assert j.sync("s", [a, b, c], META) == 2  # the append path writes just b and c
    assert len(j.trades()) == 3


def test_identical_trades_get_distinct_rows(j):
    a = trade(T, T + 60)
    assert j.sync("s", [a, a, a], META) == 3
    assert len(j.trades()) == 3
    assert j.sync("s", [a, a, a], META) == 0


def test_sessions_do_not_mix(j):
    a = trade(T, T + 60)
    j.sync("one", [a], META)
    j.sync("two", [a], META)
    assert len(j.trades()) == 2
    assert len(j.trades({"session": "one"})) == 1


def test_step_back_removes_unnoted_trades_and_hides_noted_ones(j):
    a, b, c = trade(T, T + 60), trade(T + 120, T + 180), trade(T + 240, T + 300)
    j.sync("s", [a, b, c], META)
    by_open = {r["opened"]: r["id"] for r in j.trades()}
    j.update_notes(by_open[T + 240], {"setup": "ORB", "note": "good"})  # c carries notes

    j.invalidate("s")  # user stepped back: only `a` exists any more
    j.sync("s", [a], META)
    rows = j.trades()
    assert [r["opened"] for r in rows] == [T]  # b deleted, c hidden
    hidden = j.get_trade(by_open[T + 240])
    assert hidden is not None and hidden["live"] is False and hidden["setup"] == "ORB"

    j.invalidate("s")  # replayed the same moves again: the note comes back with the trade
    j.sync("s", [a, b, c], META)
    revived = j.get_trade(by_open[T + 240])
    assert revived["live"] is True and revived["note"] == "good"
    assert len(j.trades()) == 3
    assert j.get_trade(by_open[T + 120]) is None  # b was deleted, then re-synced as a new row
    assert any(r["opened"] == T + 120 for r in j.trades())


def test_a_diverged_replay_is_detected_without_invalidate(j):
    a, b = trade(T, T + 60), trade(T + 120, T + 180)
    j.sync("s", [a, b], META)
    b2 = trade(T + 120, T + 190)  # same length, last trade differs
    j.sync("s", [a, b2], META)
    assert sorted(r["closed"] for r in j.trades()) == [T + 60, T + 190]


def test_sync_down_to_zero_trades(j):
    j.sync("s", [trade(T, T + 60)], META)
    j.invalidate("s")
    j.sync("s", [], META)
    assert j.trades() == []
    assert j.sync("s", [], META) == 0


def test_deleted_trade_is_not_revived_by_a_later_sync(j):
    a, b = trade(T, T + 60), trade(T + 120, T + 180)
    j.sync("s", [a, b], META)
    tid = j.trades()[0]["id"]
    assert j.delete_trade(tid) is True
    assert j.delete_trade(tid) is False and j.get_trade(tid) is None
    j.invalidate("s")
    j.sync("s", [a, b], META)
    assert len(j.trades()) == 1
    j.sync("s", [a, b, trade(T + 300, T + 360)], META)
    assert len(j.trades()) == 2


def test_data_persists_across_instances(tmp_path):
    p = tmp_path / "p.sqlite"
    a = jr.Journal(p)
    a.sync("s", [trade(T, T + 60)], META)
    tid = a.trades()[0]["id"]
    a.update_notes(tid, {"rating": 5})
    b = jr.Journal(p)
    assert b.get_trade(tid)["rating"] == 5
    b.sync("s", [trade(T, T + 60)], META)  # fresh process: no cache, the row is already there
    assert len(b.trades()) == 1 and b.get_trade(tid)["rating"] == 5


# ---------------- notes ----------------
def test_update_notes_round_trip_and_partial_update(j):
    j.sync("s", [trade(T, T + 60)], META)
    tid = j.trades()[0]["id"]
    t = j.update_notes(tid, {"setup": "Breakout", "mistakes": ["FOMO"], "rating": 3, "note": "n"})
    assert (t["setup"], t["mistakes"], t["rating"], t["note"]) == ("Breakout", ["FOMO"], 3, "n")
    t = j.update_notes(tid, {"emotion": "calm"})  # other fields untouched
    assert (t["setup"], t["emotion"], t["rating"]) == ("Breakout", "calm", 3)
    t = j.update_notes(tid, {"setup": None, "rating": None})
    assert t["setup"] is None and t["rating"] is None
    assert j.update_notes(99999, {"rating": 1}) is None
    with pytest.raises(ValueError):
        j.update_notes(tid, {"rating": 9})
    assert j.get_trade(tid)["emotion"] == "calm"


def test_note_text_is_stored_verbatim_not_interpreted(j):
    j.sync("s", [trade(T, T + 60)], META)
    tid = j.trades()[0]["id"]
    evil = "'); DROP TABLE trades; -- <script>alert(1)</script>"
    j.update_notes(tid, {"note": evil, "setup": "a'b"})
    assert j.get_trade(tid)["note"] == evil and j.get_trade(tid)["setup"] == "a'b"
    assert len(j.trades()) == 1


def test_excursion_dollars(j):
    j.sync("s", [trade(T, T + 60, qty=2, mae=1.5, mfe=4.0)], META)
    t = j.trades()[0]
    assert (t["mae_usd"], t["mfe_usd"]) == (1.5 * 20 * 2, 4.0 * 20 * 2)


def test_tags_lists_distinct_values(j):
    j.sync("s", [trade(T, T + 60), trade(T + 120, T + 180)], META)
    a, b = (r["id"] for r in j.trades())
    j.update_notes(a, {"setup": "ORB", "emotion": "calm", "mistakes": ["FOMO", "late"]})
    j.update_notes(b, {"setup": "ORB", "mistakes": ["FOMO"]})
    assert j.tags() == {"setups": ["ORB"], "emotions": ["calm"], "mistakes": ["FOMO", "late"]}


# ---------------- analytics ----------------
def seeded(j):
    """3 winners (+100, +300 (short), +50) and 2 losers (-100, -200), over two days."""
    d1, d2 = ts(2025, 3, 11, 14, 35), ts(2025, 3, 12, 18, 5)  # 10:35 NY Tue, 14:05 NY Wed
    t = [
        trade(d1, d1 + 600, pnl=100.0, r=1.0),
        trade(d1 + 3600, d1 + 3700, pnl=-100.0, r=-1.0),
        trade(d1 + 7200, d1 + 7300, pnl=300.0, side=Side.SHORT, r=3.0),
        trade(d2, d2 + 60, pnl=-200.0, r=-2.0),
        trade(d2 + 600, d2 + 700, pnl=50.0, r=0.5),
    ]
    j.sync("s", t, META)
    rows = j.trades()
    rows.sort(key=lambda r: r["opened"])
    j.update_notes(rows[0]["id"], {"setup": "ORB", "mistakes": []})
    j.update_notes(rows[1]["id"], {"setup": "ORB", "mistakes": ["FOMO"], "emotion": "anxious"})
    j.update_notes(rows[2]["id"], {"setup": "Reversal"})
    j.update_notes(rows[3]["id"], {"mistakes": ["FOMO", "oversize"]})
    return rows


def test_analytics_summary(j):
    seeded(j)
    s = j.analytics()["summary"]
    assert s["trades"] == 5 and s["wins"] == 3 and s["losses"] == 2
    assert s["win_rate"] == pytest.approx(0.6)
    assert s["net_pnl"] == 150.0 and s["gross_profit"] == 450.0 and s["gross_loss"] == 300.0
    assert s["profit_factor"] == pytest.approx(1.5)
    assert s["avg_win"] == pytest.approx(150.0) and s["avg_loss"] == pytest.approx(-150.0)
    assert s["expectancy"] == pytest.approx(30.0)
    assert s["avg_r"] == pytest.approx((1 - 1 + 3 - 2 + 0.5) / 5)
    assert (s["best"], s["worst"]) == (300.0, -200.0)
    # cumulative: 100, 0, 300, 100, 150 -> peak 300, trough 100
    assert s["max_drawdown"] == 200.0
    assert s["avg_hold_s"] == pytest.approx((600 + 100 + 100 + 60 + 100) / 5)
    assert s["avg_mae_usd"] == pytest.approx(20.0) and s["avg_mfe_usd"] == pytest.approx(120.0)


def test_analytics_equity_curve_is_cumulative_in_close_order(j):
    seeded(j)
    assert [e["cum"] for e in j.analytics()["equity"]] == [100.0, 0.0, 300.0, 100.0, 150.0]


def test_analytics_breakdowns(j):
    seeded(j)
    b = j.analytics()["breakdowns"]
    setups = {x["key"]: x for x in b["setup"]}
    assert setups["ORB"]["trades"] == 2 and setups["ORB"]["pnl"] == 0.0
    assert setups["Reversal"]["pnl"] == 300.0 and setups["(none)"]["trades"] == 2
    mistakes = {x["key"]: x for x in b["mistake"]}
    assert mistakes["FOMO"]["trades"] == 2 and mistakes["FOMO"]["pnl"] == -300.0
    assert mistakes["oversize"]["trades"] == 1 and "(none)" not in mistakes
    sides = {x["key"]: x for x in b["side"]}
    assert sides["short"]["pnl"] == 300.0 and sides["long"]["trades"] == 4
    hours = {x["key"]: x["trades"] for x in b["hour"]}
    assert hours == {10: 1, 11: 1, 12: 1, 14: 2}  # New York entry hours
    assert {x["key"] for x in b["weekday"]} == {1, 2}  # Tue, Wed
    assert {x["key"] for x in b["emotion"]} == {"(none)", "anxious"}


def test_analytics_filters(j):
    seeded(j)
    assert j.analytics({"start": "2025-03-12"})["summary"]["trades"] == 2
    assert j.analytics({"end": "2025-03-11"})["summary"]["trades"] == 3
    assert j.analytics({"setup": "ORB"})["summary"]["trades"] == 2
    assert j.analytics({"side": "short"})["summary"]["net_pnl"] == 300.0
    assert j.analytics({"session": "nope"})["summary"] == {"trades": 0}


def test_profit_factor_is_none_without_losses(j):
    j.sync("s", [trade(T, T + 60, pnl=10.0)], META)
    s = j.analytics()["summary"]
    assert s["profit_factor"] is None and s["avg_loss"] is None


def test_hidden_trades_are_excluded_from_analytics(j):
    a, b = trade(T, T + 60), trade(T + 120, T + 180)
    j.sync("s", [a, b], META)
    tid = next(r["id"] for r in j.trades() if r["opened"] == T + 120)
    j.update_notes(tid, {"note": "keep me"})
    j.invalidate("s")
    j.sync("s", [a], META)
    assert j.analytics()["summary"]["trades"] == 1


# ---------------- calendar ----------------
def test_calendar_groups_by_trading_day(j):
    seeded(j)
    cal = j.calendar(2025, 3)
    days = {d["day"]: d for d in cal["days"]}
    assert days["2025-03-11"]["pnl"] == 300.0 and days["2025-03-11"]["trades"] == 3
    assert days["2025-03-11"]["wins"] == 2
    assert days["2025-03-12"]["pnl"] == -150.0
    assert (cal["net_pnl"], cal["trades"], cal["green_days"], cal["red_days"]) == (150.0, 5, 1, 1)
    assert j.calendar(2025, 4)["days"] == []
    assert j.calendar(2025, 2)["trades"] == 0


def test_calendar_month_edges(j):
    late = ts(2025, 3, 31, 18)  # 14:00 NY on the 31st
    j.sync("s", [trade(late, late + 60)], META)
    assert j.calendar(2025, 3)["trades"] == 1
    assert j.calendar(2025, 12)["trades"] == 0  # december rolls the year correctly


# ---------------- storage ----------------
def test_database_is_wal_and_schema_is_stable(j):
    con = sqlite3.connect(j.path)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    assert {
        "key",
        "session_id",
        "pnl",
        "mae",
        "mfe",
        "setup",
        "mistakes",
        "note",
        "live",
        "day",
    } <= cols
    con.close()
    jr.Journal(j.path)  # opening an existing database is a no-op


def test_configure_and_get(tmp_path):
    jr.configure(tmp_path / "x" / "y.sqlite")
    assert jr.get().path == tmp_path / "x" / "y.sqlite"
    assert jr.get() is jr.get()


def test_full_resync_of_unchanged_trades_keeps_ids_and_writes_nothing(j):
    a, b = trade(T, T + 60), trade(T + 120, T + 180)
    j.sync("s", [a, b], META)
    ids = sorted(r["id"] for r in j.trades())
    for _ in range(3):  # e.g. repeated seeks in the UI
        j.invalidate("s")
        assert j.sync("s", [a, b], META) == 0
    assert sorted(r["id"] for r in j.trades()) == ids
