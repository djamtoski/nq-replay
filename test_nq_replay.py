from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from nq_replay import (
    NQ,
    Bar,
    Broker,
    ClosedTrade,
    ExecModel,
    MTFAggregator,
    Order,
    OType,
    ReplaySession,
    SessionRange,
    Side,
    active_sessions,
    stats,
)

S = 10**9


def ns(y, m, d, hh, mm=0, ss=0):
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * S)


def bar(ts, o, h, l, c, dur=S):
    return Bar(ts, o, h, l, c, 1, dur)


ZERO = ExecModel(
    latency_ms=0, slippage_ticks_market=0, slippage_ticks_stop=0, limit_requires_trade_through=False
)
T0 = ns(2025, 3, 10, 14)


def broker(model=ZERO):
    return Broker(NQ, 100_000.0, model)


def open_long(b, sl=None, tp=None, trail=None, qty=1, px=100.0):
    b.submit(T0 - S, Order(Side.LONG, qty, OType.MARKET, sl=sl, tp=tp, trail_ticks=trail))
    b.on_bar(bar(T0, px, px, px, px))
    assert b.positions, "entry did not fill"
    return next(iter(b.positions.values()))


# ---------------- fills ----------------
def test_market_fills_at_open_plus_slippage():
    b = broker(ExecModel(latency_ms=0, slippage_ticks_market=2))
    b.submit(T0 - S, Order(Side.LONG, 1, OType.MARKET))
    b.on_bar(bar(T0, 100, 101, 99, 100))
    assert next(iter(b.positions.values())).entry == 100.5


def test_latency_delays_fill_one_bar():
    b = broker(ExecModel(latency_ms=1500, slippage_ticks_market=0))
    b.submit(T0, Order(Side.LONG, 1, OType.MARKET))
    b.on_bar(bar(T0, 100, 100, 100, 100))  # closes at T0+1s < active_from
    assert not b.positions
    b.on_bar(bar(T0 + S, 101, 101, 101, 101))  # closes T0+2s >= T0+1.5s
    assert next(iter(b.positions.values())).entry == 101


def test_limit_gap_gives_price_improvement():
    b = broker()
    b.submit(T0 - S, Order(Side.LONG, 1, OType.LIMIT, price=100))
    b.on_bar(bar(T0, 98, 99, 97, 98))
    assert next(iter(b.positions.values())).entry == 98


def test_limit_trade_through_required():
    b = broker(ExecModel(latency_ms=0, limit_requires_trade_through=True))
    b.submit(T0 - S, Order(Side.LONG, 1, OType.LIMIT, price=100))
    b.on_bar(bar(T0, 101, 102, 100, 101))  # touch only
    assert not b.positions
    b.on_bar(bar(T0 + S, 101, 101, 99.75, 100))  # trades through by 1 tick
    assert next(iter(b.positions.values())).entry == 100


def test_stop_entry_gap_fills_at_open_with_slippage():
    b = broker(ExecModel(latency_ms=0, slippage_ticks_stop=1))
    b.submit(T0 - S, Order(Side.LONG, 1, OType.STOP, price=100))
    b.on_bar(bar(T0, 103, 104, 102, 103))
    assert next(iter(b.positions.values())).entry == 103.25


# ---------------- exits ----------------
def test_sl_and_tp_same_bar_sl_wins():
    b = broker()
    open_long(b, sl=95, tp=105)
    b.on_bar(bar(T0 + S, 100, 106, 94, 100))
    assert b.trades[-1].reason == "sl"
    assert b.trades[-1].exit == 95


def test_tp_gap_through_improves_fill():
    b = broker()
    open_long(b, sl=95, tp=105)
    b.on_bar(bar(T0 + S, 107, 108, 106, 107))
    t = b.trades[-1]
    assert t.reason == "tp" and t.exit == 107


def test_sl_gap_through_worsens_fill():
    b = broker()
    open_long(b, sl=95, tp=105)
    b.on_bar(bar(T0 + S, 93, 94, 92, 93))
    assert b.trades[-1].exit == 93


def test_short_side_symmetry():
    b = broker()
    b.submit(T0 - S, Order(Side.SHORT, 1, OType.MARKET, sl=105, tp=95))
    b.on_bar(bar(T0, 100, 100, 100, 100))
    b.on_bar(bar(T0 + S, 100, 101, 94, 95))
    t = b.trades[-1]
    assert t.reason == "tp" and t.pnl == 5 * 20  # trade pnl is gross
    assert b.balance == pytest.approx(100_000 + 100 - 5.0)  # net of 2 commissions


def test_pnl_and_commission_math():
    b = broker(ExecModel(latency_ms=0, slippage_ticks_market=0))
    open_long(b, sl=95, tp=110)
    b.on_bar(bar(T0 + S, 100, 111, 100, 110))
    # 10 pts * $20 = 200, minus 2 sides * 2.50
    assert b.balance == pytest.approx(100_000 + 200 - 5.0)
    assert b.trades[-1].r_mult == pytest.approx(2.0)


def test_trailing_stop_moves_up_only_and_applies_next_bar():
    b = broker()
    p = open_long(b, trail=40)  # 10 points
    b.on_bar(bar(T0 + S, 100, 120, 100, 120))
    assert p.sl == 110
    b.on_bar(bar(T0 + 2 * S, 120, 120, 115, 116))  # pullback, still above sl
    assert p.sl == 110
    b.on_bar(bar(T0 + 3 * S, 116, 117, 109, 110))
    assert b.trades[-1].reason == "sl" and b.trades[-1].exit == 110


def test_partial_close_then_full():
    b = broker()
    p = open_long(b, qty=3)
    b.close(T0, p.id, 1)
    b.on_bar(bar(T0 + S, 102, 102, 102, 102))
    assert p.qty == 2 and len(b.trades) == 1 and b.trades[0].qty == 1
    b.close(T0 + S, p.id)
    b.on_bar(bar(T0 + 2 * S, 103, 103, 103, 103))
    assert not b.positions and len(b.trades) == 2


def test_reduce_only_never_flips_position():
    b = broker()
    open_long(b, qty=1)
    b.submit(T0, Order(Side.SHORT, 5, OType.MARKET, reduce_only=True))
    b.on_bar(bar(T0 + S, 100, 100, 100, 100))
    assert not b.positions


def test_margin_rejection():
    b = Broker(NQ, 1500.0, ZERO)
    o = b.submit(T0, Order(Side.LONG, 2, OType.MARKET))
    assert o.status.value == "rejected"


def test_cancel_working_order():
    b = broker()
    o = b.submit(T0 - S, Order(Side.LONG, 1, OType.LIMIT, price=90))
    b.cancel(o.id)
    b.on_bar(bar(T0, 80, 81, 79, 80))
    assert not b.positions


# ---------------- MTF / look-ahead ----------------
def feed(agg, start, n):
    for i in range(n):
        agg.on_base_bar(bar(start + i * S, 100 + i, 101 + i, 99 + i, 100 + i))


def test_htf_bar_closes_only_after_last_base_bar():
    a = MTFAggregator([60])
    start = ns(2025, 3, 10, 14)
    feed(a, start, 59)
    assert len(a.closed(60)) == 0
    a.on_base_bar(bar(start + 59 * S, 159, 160, 158, 160))
    assert len(a.closed(60)) == 0  # last base bar emitted, but bucket only rolls on next bar
    a.on_base_bar(bar(start + 60 * S, 1, 1, 1, 1))
    assert len(a.closed(60)) == 1
    c = a.closed(60)[0]
    assert (c.o, c.h, c.l, c.c) == (100, 160, 99, 160)


def test_forming_bar_matches_cursor():
    a = MTFAggregator([300])
    start = ns(2025, 3, 10, 14)
    feed(a, start, 10)
    f = a.forming(300)
    assert f.c == 109 and f.h == 110


def test_daily_bucket_aligns_to_1800_ny():
    # EDT: 18:00 NY = 22:00 UTC
    before = ns(2025, 3, 10, 21, 59, 59)
    after = ns(2025, 3, 10, 22, 0, 0)
    assert MTFAggregator._bucket_start(after, 86400) == after
    assert MTFAggregator._bucket_start(before, 86400) == ns(2025, 3, 9, 22)


# ---------------- sessions / DST ----------------
@pytest.mark.parametrize(
    "utc, expected",
    [
        (ns(2025, 3, 7, 14, 30), "NY_AM"),  # EST: 09:30 NY
        (ns(2025, 3, 10, 13, 30), "NY_AM"),  # EDT (after Mar 9 switch): 09:30 NY
        (ns(2025, 11, 3, 14, 30), "NY_AM"),  # EST again (after Nov 2 switch)
        (ns(2025, 11, 3, 14, 29), None),
    ],
)
def test_ny_am_start_across_dst(utc, expected):
    got = active_sessions(utc)
    assert (expected in got) if expected else ("NY_AM" not in got)


def test_asia_wraps_midnight():
    assert "ASIA" in active_sessions(ns(2025, 3, 10, 3, 0))  # 23:00 NY prior evening (EDT)


def test_asia_not_active_at_1800_ny():
    assert "ASIA" not in active_sessions(ns(2025, 3, 10, 22, 0))  # 18:00 NY


def test_asia_range_accumulates_per_session_day():
    r = SessionRange("ASIA")
    t = ns(2025, 3, 10, 0, 30)  # 20:30 NY on Mar 9
    r.update(bar(t, 100, 105, 99, 101))
    r.update(bar(t + 3600 * S, 101, 107, 98, 100))
    assert (r.high, r.low) == (107, 98)
    nxt = ns(2025, 3, 11, 0, 30)  # next evening resets
    r.update(bar(nxt, 200, 201, 199, 200))
    assert (r.high, r.low) == (201, 199)


# ---------------- replay determinism ----------------
def mk_bars(n=2000):
    import random

    rnd = random.Random(3)
    px, out = 20000.0, []
    for i in range(n):
        o = px
        px = round((px + rnd.gauss(0, 1)) * 4) / 4
        out.append(bar(T0 + i * S, o, max(o, px) + 0.25, min(o, px) - 0.25, px))
    return out


def test_step_back_is_deterministic_with_and_without_snapshots():
    for snap in (50, 10**9):
        s = ReplaySession(mk_bars(), snapshot_every=snap, exec_model=ZERO)
        n = {"i": 0}

        def strat(b, ss, n=n):
            n["i"] += 1
            if n["i"] % 97 == 0:
                ss.submit(Order(Side.LONG, 1, OType.MARKET, sl=b.c - 5, tp=b.c + 5))

        s.on_bar.append(strat)
        s.step(2000)
        eq, nt = s.broker.equity(), len(s.broker.trades)
        s.step_back(1500)
        assert s.cursor == 500
        n["i"] = 500  # strategy counter must mirror the cursor
        s.step(1500)
        assert s.broker.equity() == pytest.approx(eq) and len(s.broker.trades) == nt


def test_seek_lands_on_last_closed_bar():
    s = ReplaySession(mk_bars(), exec_model=ZERO)
    s.seek(T0 + 100 * S + 500)
    assert s.cursor == 100


def test_no_future_data_reachable():
    s = ReplaySession(mk_bars(), exec_model=ZERO)
    s.step(10)
    assert s.mtf.forming(60).c == s.bars[9].c


def test_stats_basic():
    s = ReplaySession(mk_bars(), exec_model=ZERO)
    s.step(1)
    s.submit(Order(Side.LONG, 1, OType.MARKET, sl=19000, tp=100000))
    s.step(5)
    assert stats(s)["trades"] == 0


# ---------------- warm-up preload ----------------
def test_preload_builds_history_without_trading_and_sets_floor():
    s = ReplaySession(mk_bars(1200), tfs=(60,), exec_model=ZERO)
    s.preload(300)
    assert s.cursor == s.floor == 300 and len(s.mtf.closed(60)) == 4
    assert not s.broker.trades and not s.equity_curve
    s.step(100)
    s.submit(Order(Side.LONG, 1, OType.MARKET))
    s.step(50)
    s.step_back(10_000)  # clamps to the warm-up boundary, never below
    assert s.cursor == 300 and not s.broker.positions and len(s.mtf.closed(60)) == 4
    with pytest.raises(ValueError):
        s.preload(10)


def test_aggregator_matches_naive_resample():
    bars = mk_bars(1000)
    a = MTFAggregator([60, 300])
    for b in bars:
        a.on_base_bar(b)
    c = list(a.closed(60))
    assert len(c) == 16
    first = bars[:60]
    assert (c[0].o, c[0].h, c[0].l, c[0].c) == (
        first[0].o,
        max(x.h for x in first),
        min(x.l for x in first),
        first[-1].c,
    )
    assert c[0].v == sum(x.v for x in first)


# ---------------- snapshot pruning ----------------
def test_mtf_clone_is_equal_and_independent():
    a = MTFAggregator([60, 300])
    for b in mk_bars(700):
        a.on_base_bar(b)
    c = a.clone()
    assert list(c.closed(60)) == list(a.closed(60)) and c.forming(300) == a.forming(300)
    for b in mk_bars(200):  # keep feeding the original; the clone must not move
        a.on_base_bar(Bar(b.ts + 10**6 * S, b.o, b.h, b.l, b.c, b.v, S))
    assert len(c.closed(60)) == 11 and len(a.closed(60)) > 11
    c.on_base_bar(
        Bar(5 * 10**9 * S, 1, 1, 1, 1, 1, S)
    )  # and mutating the clone must not touch the original
    assert a.forming(60).ts != c.forming(60).ts


def test_snapshots_are_capped_and_evenly_spread():
    s = ReplaySession(mk_bars(4000), snapshot_every=20, max_snapshots=8, exec_model=ZERO, tfs=(60,))
    s.step(4000)
    ks = sorted(s._snaps)
    assert len(ks) <= 8
    assert ks[0] == 20  # earliest anchor kept
    assert ks[-3:] == [3960, 3980, 4000]  # newest few kept
    gaps = [b - a for a, b in zip(ks, ks[1:])]
    assert max(gaps) <= 4000 / 8 * 3  # spread stays roughly even (bounded replay distance)


def test_floor_snapshot_survives_pruning():
    s = ReplaySession(mk_bars(3000), snapshot_every=10, max_snapshots=5, exec_model=ZERO, tfs=(60,))
    s.preload(100)
    s.step(2900)
    assert min(s._snaps) == s.floor == 100 and len(s._snaps) <= 5


def test_snapshot_cap_below_the_supported_minimum_is_rejected():
    for bad in (0, 1, 4):
        with pytest.raises(ValueError, match="max_snapshots must be >= 5"):
            ReplaySession(mk_bars(10), max_snapshots=bad)
    s = ReplaySession(mk_bars(2000), snapshot_every=10, max_snapshots=5, exec_model=ZERO, tfs=(60,))
    s.step(2000)
    assert len(s._snaps) <= 5


def test_prune_invariants_hold_over_random_step_and_rewind_sequences():
    """Property check: whatever the sequence of plays and rewinds, the cap, earliest/newest retention and
    determinism of the final state hold."""
    import random

    rnd = random.Random(11)
    for trial in range(12):
        cap = rnd.choice([5, 6, 8, 12])
        every = rnd.choice([7, 25, 40])
        s = ReplaySession(
            mk_bars(3000), snapshot_every=every, max_snapshots=cap, exec_model=ZERO, tfs=(60,)
        )
        floor = rnd.choice([0, 0, 90])
        if floor:
            s.preload(floor)
        for _ in range(25):
            if rnd.random() < 0.6 and s.cursor < 2900:
                s.step(rnd.randint(1, 400))
            elif s.cursor > s.floor:
                s.step_back(rnd.randint(1, 900))
            ks = sorted(s._snaps)
            assert len(ks) <= cap, (trial, ks)
            assert all(k <= s.cursor for k in ks)  # never a snapshot from the abandoned future
            if floor:
                assert ks and ks[0] == floor  # warm-up floor is never pruned
            assert len(s.equity_curve) == s.cursor - s.floor
        fresh = ReplaySession(
            mk_bars(3000), snapshot_every=10**9, max_snapshots=5, exec_model=ZERO, tfs=(60,)
        )
        if floor:
            fresh.preload(floor)
        fresh.step(s.cursor - fresh.cursor)
        assert s.equity_curve == fresh.equity_curve and s.mtf.forming(60) == fresh.mtf.forming(60)


def test_broker_clone_is_independent_but_shares_immutable_trades():
    b = Broker(NQ, 100_000.0, ZERO)
    b.submit(T0 - S, Order(Side.LONG, 1, OType.MARKET, sl=90, tp=110))
    b.submit(T0 - S, Order(Side.LONG, 1, OType.LIMIT, price=50))
    b.on_bar(bar(T0, 100, 100, 100, 100))
    b.on_bar(bar(T0 + S, 100, 111, 100, 110))  # TP closes the position -> one closed trade
    b.submit(T0, Order(Side.SHORT, 1, OType.MARKET))
    c = b.clone()
    assert (
        c.trades is not b.trades and c.trades[0] is b.trades[0]
    )  # shared immutable contents, separate list
    c.orders[0].price = 123.0
    c.balance += 1
    c.trades.append(c.trades[0])
    assert b.orders[0].price != 123.0 and b.balance != c.balance and len(b.trades) == 1
    b.on_bar(bar(T0 + 2 * S, 100, 100, 100, 100))
    assert (
        len(c.positions) == 0 and len(b.positions) == 1
    )  # original kept running; the clone did not move
    assert (c._next_id, b._next_id) == (
        5,
        6,
    )  # ids 1-4 were issued before the clone; only the original issued 5


def test_closed_trades_are_immutable_so_sharing_them_is_safe():
    b = Broker(NQ, 100_000.0, ZERO)
    b.submit(T0 - S, Order(Side.LONG, 1, OType.MARKET, sl=90, tp=110))
    b.on_bar(bar(T0, 100, 100, 100, 100))
    b.on_bar(bar(T0 + S, 100, 111, 100, 110))
    with pytest.raises(FrozenInstanceError):
        b.trades[0].pnl = 1e9


def test_mtf_clone_carries_attributes_added_later():
    a = MTFAggregator([60])
    a.future_option = "kept"  # stands in for an attribute a later change adds to __init__
    assert a.clone().future_option == "kept"


def test_rewind_fails_loudly_if_the_equity_curve_invariant_is_violated():
    s = ReplaySession(mk_bars(500), snapshot_every=50, exec_model=ZERO, tfs=(60,))
    s.step(300)
    del s.equity_curve[10:]  # corrupt: fewer points than bars stepped
    with pytest.raises(RuntimeError, match="equity curve has"):
        s.step_back(10)


def test_snapshots_with_many_trades_share_trade_objects_instead_of_copying_them():
    s = ReplaySession(mk_bars(300), snapshot_every=1000, exec_model=ZERO, tfs=(60,))
    s.step(5)
    t = ClosedTrade(Side.LONG, 1, 1.0, 2.0, 20.0, 1.0, 0, 1, "tp")
    s.broker.trades.extend([t] * 5000)
    snap_a, snap_b = s._snapshot(), s._snapshot()
    assert snap_a[0].trades[0] is t and snap_b[0].trades[100] is t  # one object, many snapshots


@pytest.mark.parametrize("max_snaps", [5, 8])
def test_pruned_session_rewinds_exactly_like_unpruned(max_snaps):
    """Pruning only changes how far a rewind must replay, never the resulting state."""

    def run(max_snapshots):
        s = ReplaySession(
            mk_bars(3000),
            snapshot_every=25,
            max_snapshots=max_snapshots,
            exec_model=ZERO,
            tfs=(60, 300),
        )
        s.preload(60)
        s.step(200)
        s.submit(Order(Side.LONG, 2, OType.MARKET))
        s.step(300)
        (pid,) = s.broker.positions
        s.modify(pid, sl=s.bars[499].c - 6, tp=s.bars[499].c + 9)
        s.step(1500)
        out = []
        for back in (50, 700, 1200):  # several rewinds, each followed by playing forward
            s.step_back(back)
            out.append(
                (
                    s.cursor,
                    round(s.broker.equity(), 6),
                    len(s.broker.trades),
                    list(s.equity_curve[-3:]),
                )
            )
            s.step(back // 2)
            out.append(
                (s.cursor, round(s.broker.equity(), 6), len(s.broker.trades), len(s.mtf.closed(60)))
            )
        return out, s

    pruned, sp = run(max_snaps)
    full, sf = run(10**6)
    assert pruned == full
    assert len(sp._snaps) <= max_snaps < len(sf._snaps)


def test_equity_curve_is_one_point_per_stepped_bar_after_rewinds():
    s = ReplaySession(mk_bars(1500), snapshot_every=40, max_snapshots=5, exec_model=ZERO, tfs=(60,))
    s.preload(50)
    s.step(900)
    s.step_back(600)
    s.step(300)
    s.step_back(250)
    assert len(s.equity_curve) == s.cursor - s.floor
    fresh = ReplaySession(mk_bars(1500), snapshot_every=40, exec_model=ZERO, tfs=(60,))
    fresh.preload(50)
    fresh.step(s.cursor - 50)
    assert s.equity_curve == fresh.equity_curve


# ---------------- manual actions survive step_back (regression) ----------------
def manual_session(snap):
    return ReplaySession(mk_bars(), snapshot_every=snap, exec_model=ZERO)


@pytest.mark.parametrize("snap", [50, 10**9])
def test_manual_orders_survive_step_back(snap):
    def view(s):
        b = s.broker
        return (
            s.cursor,
            round(b.equity(), 6),
            round(b.balance, 6),
            len(b.trades),
            [(p.id, p.qty, p.entry, p.sl, p.tp) for p in b.positions.values()],
        )

    s = manual_session(snap)
    s.step(100)
    s.submit(Order(Side.LONG, 2, OType.MARKET))  # no SL/TP: stays open
    s.step(30)
    (pid,) = s.broker.positions
    s.modify(pid, sl=s.bars[129].c - 500, tp=None)  # logged at cursor 130
    s.step(20)
    at150 = view(s)  # state to be restored
    s.step(30)
    s.close(pid, 1)  # logged at cursor 180 (partial)
    s.step(70)
    assert len(s.broker.trades) == 1 and s.broker.positions[pid].qty == 1
    s.step_back(100)  # target 150: close is undone
    assert view(s) == at150
    assert s.broker.positions[pid].qty == 2 and s.broker.positions[pid].sl is not None
    s.step(100)  # forward again: no close happens
    assert s.broker.positions[pid].qty == 2 and not s.broker.trades
    s.step_back(s.cursor)  # to bar 0 drops every action
    assert not s.broker.positions and not s.broker.trades and s.broker.balance == 50_000.0


def test_step_back_keeps_actions_at_target_and_discards_future():
    s = manual_session(50)
    s.step(100)
    s.submit(Order(Side.LONG, 1, OType.MARKET))  # logged at cursor 100
    s.step(10)
    s.step_back(10)  # target == 100: order must still be pending
    assert len(s.broker.orders) == 1 and s.cursor == 100
    s.step(5)
    assert len(s.broker.positions) == 1
    s.step_back(60)  # target 45: order (cursor 100) is in the future
    assert not s.broker.orders and not s.actions


def test_ids_are_stable_across_replay():
    s = manual_session(10**9)
    s.step(10)
    s.submit(Order(Side.LONG, 2, OType.MARKET))
    s.step(5)
    pid = next(iter(s.broker.positions))
    s.step_back(3)
    s.step(3)
    assert pid in s.broker.positions
    s.close(pid, 1)  # id from before the replay still valid
    s.step(2)
    assert s.broker.positions[pid].qty == 1


# ---------------- MAE / MFE (journal) ----------------
def test_excursions_are_tracked_from_the_bar_after_entry_and_include_the_exit_bar():
    b = broker()
    b.submit(T0 - S, Order(Side.LONG, 1, OType.MARKET, tp=105))
    b.on_bar(
        bar(T0, 100, 110, 90, 100)
    )  # entry bar: its range happened before/at the fill, not counted
    p = next(iter(b.positions.values()))
    assert (p.mae, p.mfe) == (0.0, 0.0)
    b.on_bar(bar(T0 + S, 100, 103, 99, 102))
    assert (p.mae, p.mfe) == (1.0, 3.0)
    b.on_bar(bar(T0 + 2 * S, 102, 106, 100, 105))  # take profit hit on this bar
    t = b.trades[-1]
    assert (t.reason, t.mae, t.mfe) == ("tp", 1.0, 6.0)


def test_short_excursions_are_mirrored():
    b = broker()
    b.submit(T0 - S, Order(Side.SHORT, 1, OType.MARKET))
    b.on_bar(bar(T0, 100, 100, 100, 100))
    p = next(iter(b.positions.values()))
    b.on_bar(bar(T0 + S, 100, 101, 96, 97))
    assert (p.mae, p.mfe) == (1.0, 4.0)


def test_excursions_survive_partial_close_and_snapshots():
    s = ReplaySession(
        [bar(T0 + i * S, 100, 100 + i % 5, 99, 100) for i in range(300)],
        NQ,
        exec_model=ZERO,
        snapshot_every=50,
    )
    s.step(10)
    s.submit(Order(Side.LONG, 2, OType.MARKET))
    s.step(20)
    pid = next(iter(s.broker.positions))
    s.close(pid, 1)
    s.step(2)  # the close is a market order: it fills on the next bar
    t = s.broker.trades[-1]
    assert t.qty == 1 and t.mfe == 4.0 and t.mae == 1.0
    s.step_back(5)  # restored positions keep their excursions
    s.step(5)
    assert next(iter(s.broker.positions.values())).mfe == 4.0
