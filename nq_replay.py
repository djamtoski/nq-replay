"""
nq_replay.py - minimal, dependency-light replay/backtest engine for NQ / MNQ futures.

Modules (all in one file for readability; split along the section banners):
  1. Instrument + data schema
  2. Session windows (America/New_York, DST-safe)
  3. MTF aggregator (no look-ahead)
  4. Broker / execution simulator
  5. Replay clock + event loop
  6. Account statistics

Base feed = 1-second (or 1-minute) OHLCV bars, UTC epoch-ns timestamps.
Conservative intrabar rule: when SL and TP are both inside one base bar, SL fills first.
"""

from __future__ import annotations

import copy
import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import Enum
from typing import NamedTuple
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# 1. Instrument + data schema
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick_size: float
    point_value: float  # USD per 1.0 index point
    init_margin: float  # per contract, intraday
    commission_per_side: float  # per contract

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.point_value

    def round_px(self, px: float) -> float:
        return round(round(px / self.tick_size) * self.tick_size, 10)


NQ = Instrument("NQ", 0.25, 20.0, 1000.0, 2.50)
MNQ = Instrument("MNQ", 0.25, 2.0, 100.0, 0.75)


class Bar(NamedTuple):
    """Immutable bar. A NamedTuple (not a dataclass): ~8x cheaper to build, which matters when
    warm-up and synthetic data create hundreds of thousands of them."""

    ts: int  # bar OPEN time, epoch ns UTC
    o: float
    h: float
    l: float
    c: float
    v: float
    dur_ns: int  # bar length; close time = ts + dur_ns

    @property
    def close_ts(self) -> int:
        return self.ts + self.dur_ns


# Parquet layout (one file per symbol/day, sorted by ts):
#   bars/{symbol}/{res}/{YYYY}/{YYYY-MM-DD}.parquet
#   columns: ts:int64 (open, ns UTC) | o,h,l,c:float64 | v:int64 | instrument_id:int32
# Continuous contract: store raw-contract bars + a roll table (date, from_contract, to_contract,
# back-adjust gap); apply adjustment at read time, never at write time.


# --------------------------------------------------------------------------- #
# 2. Session windows
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionDef:
    name: str
    start: time  # New York wall-clock
    end: time  # may be < start => wraps past midnight


DEFAULT_SESSIONS = (
    SessionDef("ASIA", time(20, 0), time(0, 0)),
    SessionDef("LONDON", time(2, 0), time(5, 0)),
    SessionDef("NY_AM", time(9, 30), time(12, 0)),
    SessionDef("NY_PM", time(13, 30), time(16, 0)),
)


def active_sessions(ts_ns: int, defs: Iterable[SessionDef] = DEFAULT_SESSIONS) -> list[str]:
    t = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(NY).time()
    out = []
    for d in defs:
        inside = d.start <= t < d.end if d.start < d.end else (t >= d.start or t < d.end)
        if inside:
            out.append(d.name)
    return out


@dataclass
class SessionRange:
    """Running high/low of a session (e.g. Asian range), only from bars already emitted."""

    name: str
    high: float = -math.inf
    low: float = math.inf
    day: int | None = None

    def update(self, bar: Bar, defs=DEFAULT_SESSIONS) -> None:
        if self.name not in active_sessions(bar.ts, defs):
            return
        local = datetime.fromtimestamp(bar.ts / 1e9, tz=UTC).astimezone(NY)
        # Asia opens 20:00 - attribute evening bars to the next trading date
        key = local.date().toordinal() + (1 if local.hour >= 18 else 0)
        if key != self.day:
            self.day, self.high, self.low = key, -math.inf, math.inf
        self.high, self.low = max(self.high, bar.h), min(self.low, bar.l)


# --------------------------------------------------------------------------- #
# 3. MTF aggregator - no look-ahead
# --------------------------------------------------------------------------- #
class MTFAggregator:
    """
    Builds higher-timeframe bars from base bars as they are replayed.
    - `closed(tf)`: completed HTF bars - safe for signals.
    - `forming(tf)`: partial bar up to the cursor - what a live chart shows.
    A HTF bar is only moved to `closed` when the base bar that ends it has been emitted.
    Buckets are aligned to epoch (1D buckets align to 18:00 NY, the CME futures day).
    """

    def __init__(self, tfs_seconds: Iterable[int], keep: int = 5000):
        self.tfs = sorted(set(tfs_seconds))
        self._closed: dict[int, deque] = {tf: deque(maxlen=keep) for tf in self.tfs}
        # forming bar per tf as a mutable list [start, end, o, h, l, c, v]; updated in place
        # (this is the hot path: warm-up feeds hundreds of thousands of bars through it)
        self._f: dict[int, list | None] = {tf: None for tf in self.tfs}

    @staticmethod
    def _bucket(ts_ns: int, tf_s: int) -> tuple[int, int]:
        """(start, end) of the bucket containing ts. 1D buckets run 18:00 NY -> 18:00 NY."""
        tf_ns = tf_s * 1_000_000_000
        if tf_s < 86_400:
            start = ts_ns - ts_ns % tf_ns
            return start, start + tf_ns
        local = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).astimezone(NY)
        day0 = local.replace(hour=18, minute=0, second=0, microsecond=0)
        if local < day0:
            day0 -= _ONE_DAY
        return int(day0.timestamp() * 1e9), int((day0 + _ONE_DAY).timestamp() * 1e9)

    @staticmethod
    def _bucket_start(ts_ns: int, tf_s: int) -> int:
        return MTFAggregator._bucket(ts_ns, tf_s)[0]

    def on_base_bar(self, b: Bar) -> None:
        ts = b.ts
        for tf in self.tfs:
            f = self._f[tf]
            if f is not None and ts < f[1]:  # same bucket: update in place
                if b.h > f[3]:
                    f[3] = b.h
                if b.l < f[4]:
                    f[4] = b.l
                f[5] = b.c
                f[6] += b.v
                continue
            if f is not None:  # bucket rolled: the old bar is now closed
                self._closed[tf].append(Bar(f[0], f[2], f[3], f[4], f[5], f[6], f[1] - f[0]))
            start, end = self._bucket(ts, tf)
            self._f[tf] = [start, end, b.o, b.h, b.l, b.c, b.v]

    def clone(self) -> MTFAggregator:
        """
        Independent copy for snapshots. Bars are immutable tuples, so the closed-bar deques are copied
        shallowly (sharing the Bar objects between snapshots); only the mutable forming lists are
        duplicated. Measured on 7 timeframes x 5000 bars: 0.08 ms vs 35 ms for copy.deepcopy.
        Starts from copy.copy so any attribute added to the class later is carried over automatically;
        only the members that are mutated in place are replaced.
        """
        c = copy.copy(self)
        c._closed = {tf: deque(d, maxlen=d.maxlen) for tf, d in self._closed.items()}
        c._f = {tf: (None if f is None else list(f)) for tf, f in self._f.items()}
        return c

    def closed(self, tf: int) -> deque[Bar]:
        return self._closed[tf]

    def forming(self, tf: int) -> Bar | None:
        f = self._f[tf]
        return None if f is None else Bar(f[0], f[2], f[3], f[4], f[5], f[6], f[1] - f[0])


from datetime import timedelta  # noqa: E402

_ONE_DAY = timedelta(days=1)


# --------------------------------------------------------------------------- #
# 4. Broker / execution simulator
# --------------------------------------------------------------------------- #
class Side(Enum):
    LONG = 1
    SHORT = -1


class OType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OStatus(Enum):
    PENDING = "pending"  # accepted, waiting out latency
    WORKING = "working"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    side: Side
    qty: int
    otype: OType
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    trail_ticks: int | None = None
    reduce_only: bool = False
    id: int = 0  # assigned by Broker.submit (per-account counter => replay-stable)
    status: OStatus = OStatus.PENDING
    active_from_ns: int = 0  # latency gate
    parent_pos: int | None = None


@dataclass
class Position:
    side: Side
    qty: int
    entry: float
    sl: float | None
    tp: float | None
    trail_ticks: int | None
    opened_ns: int
    id: int = 0  # assigned by Broker._apply_fill
    best: float = 0.0  # best price since entry (for trailing)
    risk_pts: float = 0.0  # initial |entry-sl|, for R multiples
    realized: float = 0.0
    mae: float = 0.0  # worst excursion against the entry so far, in points (>= 0)
    mfe: float = 0.0  # best excursion in favour of the entry so far, in points (>= 0)


@dataclass(frozen=True)
class ExecModel:
    latency_ms: int = 150  # order -> venue delay, applied on replay clock
    slippage_ticks_market: int = 1  # adverse, market + stop fills
    slippage_ticks_stop: int = 1
    limit_requires_trade_through: bool = True  # touch isn't enough: price must trade 1 tick through
    half_spread_ticks: float = (
        0.0  # NQ futures: quoted from 1-tick spread; use 0 with bars + slippage
    )
    max_fill_frac_of_volume: float = 1.0  # partial-fill cap vs bar volume


@dataclass(
    frozen=True
)  # immutable on purpose: Broker.clone() shares trade objects between snapshots
class ClosedTrade:
    side: Side
    qty: int
    entry: float
    exit: float
    pnl: float
    r_mult: float | None
    opened_ns: int
    closed_ns: int
    reason: str
    mae: float = 0.0  # points; bar-level (a bar's high/low, order within the bar unknown)
    mfe: float = 0.0


class Broker:
    def __init__(
        self, inst: Instrument, balance: float = 50_000.0, exec_model: ExecModel = ExecModel()
    ):
        self.inst, self.x = inst, exec_model
        self.balance = balance
        self.orders: list[Order] = []
        self.positions: dict[int, Position] = {}
        self.trades: list[ClosedTrade] = []
        self.last_price: float = float("nan")
        self._next_id = 1

    def clone(self) -> Broker:
        """
        Independent copy for snapshots. Orders and positions are mutated in place so they are copied;
        closed trades are frozen, so the list is copied but its (potentially huge) contents are shared.
        copy.deepcopy was O(trades): 88 ms per snapshot/restore at 20,000 closed trades.
        """
        c = copy.copy(self)  # carries every scalar and any attribute added later
        c.orders = [copy.copy(o) for o in self.orders]
        c.positions = {k: copy.copy(p) for k, p in self.positions.items()}
        c.trades = list(self.trades)
        return c

    # ---- public API (called by UI / strategy at replay-clock time `now`) ----
    def submit(self, now: int, o: Order) -> Order:
        o.id, self._next_id = self._next_id, self._next_id + 1
        if o.qty <= 0 or (o.otype != OType.MARKET and o.price is None):
            o.status = OStatus.REJECTED
            return o
        need = self.margin_used() + (0 if o.reduce_only else o.qty * self.inst.init_margin)
        if need > self.equity():
            o.status = OStatus.REJECTED
            return o
        if o.price is not None:
            o.price = self.inst.round_px(o.price)
        o.active_from_ns = now + self.x.latency_ms * 1_000_000
        self.orders.append(o)
        return o

    def cancel(self, order_id: int) -> None:
        for o in self.orders:
            if o.id == order_id and o.status in (OStatus.PENDING, OStatus.WORKING):
                o.status = OStatus.CANCELLED

    def modify_sl_tp(self, pos_id: int, sl: float | None, tp: float | None) -> None:
        p = self.positions[pos_id]
        p.sl = self.inst.round_px(sl) if sl is not None else None
        p.tp = self.inst.round_px(tp) if tp is not None else None

    def close(self, now: int, pos_id: int, qty: int | None = None) -> None:
        """Partial or full close at market on the next bar (same latency/slippage as any market)."""
        p = self.positions[pos_id]
        side = Side.SHORT if p.side == Side.LONG else Side.LONG
        self.submit(
            now, Order(side, qty or p.qty, OType.MARKET, reduce_only=True, parent_pos=pos_id)
        )

    # ---- account maths ----
    def unrealized(self) -> float:
        if math.isnan(self.last_price):
            return 0.0
        return sum(
            (self.last_price - p.entry) * p.side.value * p.qty * self.inst.point_value
            for p in self.positions.values()
        )

    def equity(self) -> float:
        return self.balance + self.unrealized()

    def margin_used(self) -> float:
        return sum(p.qty for p in self.positions.values()) * self.inst.init_margin

    # ---- core: process one base bar ----
    def on_bar(self, bar: Bar) -> None:
        self.last_price = bar.c
        for p in (
            self.positions.values()
        ):  # excursions, incl. the bar that exits (opened on an earlier bar)
            up, down = bar.h - p.entry, p.entry - bar.l
            fav, adv = (up, down) if p.side.value == 1 else (down, up)
            p.mfe, p.mae = max(p.mfe, fav), max(p.mae, adv)
        # 1) resting protective exits on existing positions (SL first = conservative)
        for p in list(self.positions.values()):
            self._check_exits(p, bar)  # exits first: a trail moved by this bar's
            if p.id in self.positions and p.trail_ticks:  # extreme must not stop out this bar
                self._trail(p, bar)
        # 2) entry / generic orders
        for o in list(self.orders):
            if (
                o.status not in (OStatus.PENDING, OStatus.WORKING)
                or bar.close_ts <= o.active_from_ns
            ):
                continue
            o.status = OStatus.WORKING
            fill = self._try_fill(o, bar)
            if fill is not None:
                self._apply_fill(o, fill, bar)
        self.orders = [o for o in self.orders if o.status in (OStatus.PENDING, OStatus.WORKING)]

    def _try_fill(self, o: Order, bar: Bar) -> float | None:
        t, s = self.inst.tick_size, o.side.value
        if o.otype == OType.MARKET:
            # fill at the open of the first bar that closes after the latency gate
            base = bar.o if bar.ts >= o.active_from_ns else bar.c
            return self.inst.round_px(
                base + s * (self.x.slippage_ticks_market + self.x.half_spread_ticks) * t
            )
        if o.otype == OType.LIMIT:
            assert o.price is not None
            need = t if self.x.limit_requires_trade_through else 0.0
            hit = bar.l <= o.price - need if o.side == Side.LONG else bar.h >= o.price + need
            if not hit:
                return None
            # gap through the limit => price improvement at open
            gap = bar.o < o.price if o.side == Side.LONG else bar.o > o.price
            return bar.o if gap else o.price
        if o.otype == OType.STOP:
            assert o.price is not None
            hit = bar.h >= o.price if o.side == Side.LONG else bar.l <= o.price
            if not hit:
                return None
            base = max(bar.o, o.price) if o.side == Side.LONG else min(bar.o, o.price)
            return self.inst.round_px(base + s * self.x.slippage_ticks_stop * t)
        return None

    def _apply_fill(self, o: Order, px: float, bar: Bar) -> None:
        o.status = OStatus.FILLED
        self.balance -= self.inst.commission_per_side * o.qty
        # reducing an existing opposite position?
        opp = [p for p in self.positions.values() if p.side != o.side]
        if opp and (o.reduce_only or True):
            remaining = o.qty
            for p in sorted(opp, key=lambda p: p.opened_ns):
                if remaining == 0:
                    break
                q = min(remaining, p.qty)
                self._realize(p, q, px, bar.close_ts, "manual" if o.reduce_only else "opposite")
                remaining -= q
            if remaining == 0 or o.reduce_only:
                return
            o.qty = remaining
        pos = Position(o.side, o.qty, px, o.sl, o.tp, o.trail_ticks, bar.close_ts, best=px)
        pos.risk_pts = abs(px - o.sl) if o.sl is not None else 0.0
        pos.id, self._next_id = self._next_id, self._next_id + 1
        self.positions[pos.id] = pos

    def _check_exits(self, p: Position, bar: Bar) -> None:
        s = p.side.value
        sl_hit = p.sl is not None and ((bar.l <= p.sl) if s == 1 else (bar.h >= p.sl))
        tp_hit = p.tp is not None and ((bar.h >= p.tp) if s == 1 else (bar.l <= p.tp))
        if sl_hit:  # SL wins ties
            assert p.sl is not None
            base = min(bar.o, p.sl) if s == 1 else max(bar.o, p.sl)
            px = self.inst.round_px(base - s * self.x.slippage_ticks_stop * self.inst.tick_size)
            self._realize(p, p.qty, px, bar.close_ts, "sl")
        elif tp_hit:
            assert p.tp is not None
            base = max(bar.o, p.tp) if s == 1 else min(bar.o, p.tp)  # gap-through improves fill
            self._realize(p, p.qty, base, bar.close_ts, "tp")

    def _trail(self, p: Position, bar: Bar) -> None:
        assert p.trail_ticks is not None
        s, d = p.side.value, p.trail_ticks * self.inst.tick_size
        p.best = max(p.best, bar.h) if s == 1 else min(p.best, bar.l)
        new_sl = p.best - s * d
        if p.sl is None or (new_sl > p.sl if s == 1 else new_sl < p.sl):
            p.sl = self.inst.round_px(
                new_sl
            )  # takes effect from the NEXT bar (set after exit check order)

    def _realize(self, p: Position, qty: int, px: float, ts: int, reason: str) -> None:
        pnl = (px - p.entry) * p.side.value * qty * self.inst.point_value
        self.balance += pnl - (self.inst.commission_per_side * qty if reason in ("sl", "tp") else 0)
        r = (pnl / (p.risk_pts * self.inst.point_value * qty)) if p.risk_pts else None
        self.trades.append(
            ClosedTrade(p.side, qty, p.entry, px, pnl, r, p.opened_ns, ts, reason, p.mae, p.mfe)
        )
        p.qty -= qty
        if p.qty == 0:
            self.positions.pop(p.id, None)


# --------------------------------------------------------------------------- #
# 5. Replay clock + event loop
# --------------------------------------------------------------------------- #
class Clock(Enum):
    PAUSED = 0
    PLAYING = 1


class ReplaySession:
    """
    Cursor-based replay. The cursor is the index of the NEXT unrevealed base bar, so
    nothing at or after it is reachable by strategy/UI code (look-ahead is structurally impossible).
    Step-back rebuilds state from a periodic snapshot instead of un-applying fills.
    """

    cursor: int
    broker: Broker
    mtf: MTFAggregator
    asia: SessionRange

    def __init__(
        self,
        bars: list[Bar],
        inst: Instrument = NQ,
        tfs=(60, 300, 900, 3600, 86400),
        exec_model: ExecModel = ExecModel(),
        balance=50_000.0,
        snapshot_every=3600,
        max_snapshots=24,
    ):
        self.bars, self.inst = bars, inst
        self.tfs, self.exec_model, self.balance0 = tfs, exec_model, balance
        self.snapshot_every = snapshot_every
        # Bounds memory; spacing stays roughly even (see _prune). At least 5 because _prune always keeps the
        # earliest + newest 3, so a smaller cap could not be honoured. The worst-case step_back replay is about
        # total_bars / max_snapshots, so raise it for very long datasets (memory per snapshot is roughly constant).
        if max_snapshots < 5:
            raise ValueError(f"max_snapshots must be >= 5, got {max_snapshots}")
        self.max_snapshots = max_snapshots
        self.state = Clock.PAUSED
        self.speed = 1.0
        self.on_bar: list[Callable[[Bar, ReplaySession], None]] = []
        self._snaps: dict[int, tuple] = {}
        self.actions: list[
            tuple[int, str, tuple]
        ] = []  # (cursor, kind, args) - replayed on step_back
        self._replaying = False
        self.floor = 0  # first cursor the user may rewind to (end of warm-up history)
        self._reset()

    def preload(self, n: int) -> None:
        """
        Reveal the first `n` bars as read-only history (chart context for every timeframe).
        Fast path: feeds only the aggregators + session range, no fills/equity. Rewinding never
        goes below this point.
        """
        if self.cursor or n <= 0:
            raise ValueError("preload must be called once, on a fresh session, with n > 0")
        n = min(n, len(self.bars))
        asia_from = max(0, n - 130_000)  # ~36h: enough for the current Asian range
        for i in range(n):
            b = self.bars[i]
            self.mtf.on_base_bar(b)
            if i >= asia_from:
                self.asia.update(b)
        self.cursor = self.floor = n
        self.broker.last_price = self.bars[n - 1].c
        self._store_snapshot()

    def _snapshot(self) -> tuple:
        # The equity curve is NOT stored: it is append-only with exactly one point per stepped bar
        # (len == cursor - floor), and actions before a retained snapshot never change, so its prefix is
        # always still valid and a restore just truncates it. That removes an O(n) copy per snapshot.
        return (self.broker.clone(), self.mtf.clone(), copy.deepcopy(self.asia))

    def _store_snapshot(self) -> None:
        self._snaps[self.cursor] = self._snapshot()
        self._prune()

    def _prune(self) -> None:
        """
        Keep at most `max_snapshots`. Repeatedly drop the snapshot whose removal leaves the smallest gap
        between its neighbours, so retained snapshots stay evenly spread over the whole run (worst-case
        step_back replay ~ total_bars / max_snapshots) instead of memory growing with run length. The
        earliest snapshot (the warm-up floor when preload() was used, otherwise the first boundary) and the
        newest few are never dropped.
        """
        keep_recent = 3
        while len(self._snaps) > self.max_snapshots:
            ks = sorted(self._snaps)
            cand = range(1, len(ks) - keep_recent)  # exclude earliest and the newest `keep_recent`
            if not cand:
                return
            victim = min(cand, key=lambda i: ks[i + 1] - ks[i - 1])
            del self._snaps[ks[victim]]

    def _reset(self) -> None:
        self.cursor = 0
        self.broker = Broker(self.inst, self.balance0, self.exec_model)
        self.mtf = MTFAggregator(self.tfs)
        self.asia = SessionRange("ASIA")
        self.equity_curve: list[tuple[int, float]] = []

    @property
    def now(self) -> int:
        """Replay time = close time of the last revealed base bar."""
        return self.bars[self.cursor - 1].close_ts if self.cursor else self.bars[0].ts

    def step(self, n: int = 1) -> None:
        for _ in range(n):
            if self.cursor >= len(self.bars):
                return
            b = self.bars[self.cursor]
            self.cursor += 1
            self.broker.on_bar(b)
            self.mtf.on_base_bar(b)
            self.asia.update(b)
            self.equity_curve.append((b.close_ts, self.broker.equity()))
            # snapshot BEFORE callbacks: anything done at this cursor (by user or callback)
            # lives in the action log and is re-applied after a restore.
            if self.cursor % self.snapshot_every == 0:
                self._store_snapshot()
            for cb in self.on_bar:
                cb(b, self)

    # ---- user / strategy actions: always go through these so step_back can replay them ----
    # (strategy callbacks must use these too, not ss.broker directly; during a replay they are no-ops
    #  because the logged action is re-applied instead.)
    def submit(self, order: Order) -> Order:
        if self._replaying:
            return order
        self.actions.append((self.cursor, "submit", (copy.copy(order),)))
        return self.broker.submit(self.now, order)

    def cancel(self, order_id: int) -> None:
        if not self._replaying:
            self.actions.append((self.cursor, "cancel", (order_id,)))
            self.broker.cancel(order_id)

    def modify(self, pos_id: int, sl: float | None, tp: float | None) -> None:
        if not self._replaying:
            if pos_id not in self.broker.positions:
                raise KeyError(pos_id)
            self.actions.append((self.cursor, "modify", (pos_id, sl, tp)))
            self.broker.modify_sl_tp(pos_id, sl, tp)

    def close(self, pos_id: int, qty: int | None = None) -> None:
        if not self._replaying:
            if pos_id not in self.broker.positions:
                raise KeyError(pos_id)
            self.actions.append((self.cursor, "close", (pos_id, qty)))
            self.broker.close(self.now, pos_id, qty)

    def _apply_logged(self, cursor: int) -> None:
        for c, kind, args in self.actions:
            if c != cursor:
                continue
            if kind == "submit":
                self.broker.submit(self.now, copy.copy(args[0]))
            elif kind == "cancel":
                self.broker.cancel(*args)
            elif kind == "modify":
                self.broker.modify_sl_tp(*args)
            elif kind == "close":
                self.broker.close(self.now, *args)

    def _rewind_to(self, target: int) -> None:
        """Rebuild state at `target` from the nearest earlier snapshot, replaying logged actions.
        Actions and snapshots from the abandoned future are discarded (undo semantics)."""
        target = max(target, self.floor)
        self.actions = [a for a in self.actions if a[0] <= target]
        for k in [k for k in self._snaps if k > target]:
            del self._snaps[k]
        snap_at = max(self._snaps, default=0)
        if snap_at:
            self.cursor = snap_at
            b, m, a = self._snaps[snap_at]
            self.broker, self.mtf, self.asia = b.clone(), m.clone(), copy.deepcopy(a)
            keep = snap_at - self.floor
            if (
                len(self.equity_curve) < keep
            ):  # invariant: one point per stepped bar (see _snapshot)
                raise RuntimeError(
                    f"equity curve has {len(self.equity_curve)} points, expected at least {keep}"
                )
            del self.equity_curve[keep:]  # the prefix is still valid
        else:
            self._reset()
        self._replaying = True
        try:
            while self.cursor < target:
                self._apply_logged(self.cursor)
                self.step(1)
            self._apply_logged(target)
        finally:
            self._replaying = False

    def step_back(self, n: int = 1) -> None:
        self._rewind_to(max(0, self.cursor - n))

    def seek(self, ts_ns: int) -> None:
        """'Replay to this date' - binary search then fast-forward from nearest snapshot."""
        lo, hi = 0, len(self.bars)
        while lo < hi:
            mid = (lo + hi) // 2
            lo, hi = (mid + 1, hi) if self.bars[mid].close_ts <= ts_ns else (lo, mid)
        if lo < self.cursor:
            self.step_back(self.cursor - lo)
        else:
            self.step(lo - self.cursor)

    async def run(self, sleep=None) -> None:
        """Wall-clock-paced playback: wait = bar duration / speed."""
        import asyncio

        sleep = sleep or asyncio.sleep
        self.state = Clock.PLAYING
        while self.state is Clock.PLAYING and self.cursor < len(self.bars):
            self.step()
            await sleep(self.bars[self.cursor - 1].dur_ns / 1e9 / self.speed)

    def pause(self) -> None:
        self.state = Clock.PAUSED


# --------------------------------------------------------------------------- #
# 6. Account statistics
# --------------------------------------------------------------------------- #
def stats(session: ReplaySession) -> dict:
    tr = session.broker.trades
    if not tr:
        return {"trades": 0}
    wins = [t for t in tr if t.pnl > 0]
    losses = [t for t in tr if t.pnl <= 0]
    peak, mdd = -math.inf, 0.0
    for _, e in session.equity_curve:
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
    rs = [t.r_mult for t in tr if t.r_mult is not None]
    gp, gl = sum(t.pnl for t in wins), -sum(t.pnl for t in losses)
    return {
        "trades": len(tr),
        "win_rate": len(wins) / len(tr),
        "net_pnl": sum(t.pnl for t in tr),
        "profit_factor": gp / gl if gl else math.inf,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "max_drawdown_usd": mdd,
        "equity": session.broker.equity(),
    }


# --------------------------------------------------------------------------- #
# Demo / self-check:  python nq_replay.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import random

    random.seed(7)
    start = int(datetime(2025, 3, 10, 13, 0, tzinfo=UTC).timestamp() * 1e9)  # 09:00 NY (EDT)
    px, bars = 20000.0, []
    for i in range(3 * 3600):  # 3 hours of 1s bars
        o = px
        px = round((px + random.gauss(0, 0.6)) * 4) / 4
        bars.append(Bar(start + i * 10**9, o, max(o, px) + 0.25, min(o, px) - 0.25, px, 5, 10**9))

    s = ReplaySession(bars, NQ, tfs=(60, 300, 900))
    entered: list = []

    def strat(b: Bar, ss: ReplaySession):
        c5 = ss.mtf.closed(300)  # closed 5m bars only => no look-ahead
        if (
            len(c5) >= 3
            and not ss.broker.positions
            and not entered
            and "NY_AM" in active_sessions(b.ts)
        ):
            last = c5[-1].c
            ss.submit(Order(Side.LONG, 1, OType.MARKET, sl=last - 10, tp=last + 20))
            entered.append(1)
            if len(entered) > 5:
                return

    s.on_bar.append(strat)
    s.step(len(bars))
    print(stats(s))
    # look-ahead guard: the forming 5m bar never contains data past the cursor
    f = s.mtf.forming(300)
    assert f is None or f.ts + f.dur_ns >= s.now - 300 * 10**9
    # step-back determinism
    eq_end = s.broker.equity()
    s.step_back(1800)
    s.step(1800)
    assert abs(s.broker.equity() - eq_end) < 1e-6, "replay not deterministic"
    print("ok: deterministic after step_back/forward;", "asia range:", s.asia.high, s.asia.low)
