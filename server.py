"""
server.py - FastAPI + WebSocket front end for ReplaySession.

Run:   python -m uvicorn server:app --host 127.0.0.1 --port 8000
REST:  POST /sessions      create a session (synthetic or parquet data)
       GET  /sessions/{id} state snapshot      DELETE /sessions/{id}
WS:    /ws/{id}            commands in, events out (see Cmd* models and _tick/_reset)

Look-ahead safety: only bars below the replay cursor are ever serialized.
Binds to localhost by default; WebSocket Origin is checked so a random web page in your
browser can't drive a local session (cross-site WebSocket hijacking).
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Annotated, Literal, Optional, Union
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from nq_replay import (MNQ, NQ, Bar, Broker, ExecModel, Order, OType, ReplaySession, Side, Clock, stats)

ALLOWED_ORIGINS = {"http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173",
                   "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"}
MAX_SESSIONS = 20
FRAME_S = 0.05            # playback frame; bars/frame = elapsed * speed / bar_duration
MAX_BARS_PER_FRAME = 2000
HISTORY = 500             # closed bars per timeframe sent on reset
S = 10**9
NY = ZoneInfo("America/New_York")
log = logging.getLogger("replay")
DEFAULT_TFS = [1, 15, 60, 300, 900, 3600, 14400]


# --------------------------------------------------------------------------- data sources
def synthetic_bars(n: int = 6 * 3600, start_utc: str = "2025-03-10T13:00:00+00:00", seed: int = 1) -> list[Bar]:
    """Short flat series (used by tests / quick demos). No warm-up."""
    rnd = random.Random(seed)
    t0 = int(datetime.fromisoformat(start_utc).timestamp() * S)
    px, out = 20000.0, []
    for i in range(n):
        o, px = px, round((px + rnd.gauss(0, 0.6)) * 4) / 4
        out.append(Bar(t0 + i * S, o, max(o, px) + .25, min(o, px) - .25, px, rnd.randint(1, 20), S))
    return out


@lru_cache(maxsize=2)
def synthetic_days(seed: int = 1) -> list[Bar]:
    """
    Three CME trading days of 1s bars (Thu 2025-03-06, Fri 03-07, Mon 03-10), 18:00 -> 17:00 NY each.
    Volatility follows a rough intraday profile (quiet Asia, busier London, loud NY open).
    Cached and shared read-only across sessions.
    """
    rnd = random.Random(seed)
    out: list[Bar] = []
    px = 20000.0
    for d in (date(2025, 3, 6), date(2025, 3, 7), date(2025, 3, 10)):
        start = datetime.combine(d - timedelta(days=1), time(18), tzinfo=NY).astimezone(timezone.utc)
        end = datetime.combine(d, time(17), tzinfo=NY).astimezone(timezone.utc)
        t = int(start.timestamp())
        t_end = int(end.timestamp())
        trend, sigma = 0.0, 0.3
        while t < t_end:
            if t % 600 == 0 or t == int(start.timestamp()):       # re-evaluate every 10 minutes
                local_h = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(NY).hour
                sigma = 0.9 if local_h in (9, 10) else 0.55 if (2 <= local_h < 5 or 11 <= local_h < 16) else 0.3
                trend = rnd.gauss(0, 0.04)                          # slow drift
            o, px = px, round((px + trend + rnd.gauss(0, sigma)) * 4) / 4
            out.append(Bar(t * S, o, max(o, px) + (0.25 if rnd.random() < .5 else 0),
                           min(o, px) - (0.25 if rnd.random() < .5 else 0), px, rnd.randint(1, 20), S))
            t += 1
    return out


def start_index(bars: list[Bar], day: date, hhmm: str) -> int:
    """Index of the first bar at/after `day hh:mm` New York time (binary search on bar open time)."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    target = int(datetime.combine(day, time(hh, mm), tzinfo=NY).timestamp() * S)
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (mid + 1, hi) if bars[mid].ts < target else (lo, mid)
    return lo


def _clean(x):
    """Make a message valid JSON: NaN/Infinity (e.g. profit factor with no losing trade) -> null.
    Browsers' JSON.parse rejects them, which silently froze the UI after the first all-winning stretch."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


# --------------------------------------------------------------------------- wire models
class CreateSession(BaseModel):
    source: Literal["synthetic", "parquet"] = "synthetic"
    instrument: Literal["NQ", "MNQ"] = "NQ"
    symbol: str = "NQ.v.0"
    start: Optional[date] = None
    end: Optional[date] = None
    back_adjust: bool = False
    balance: float = Field(50_000.0, gt=0)
    bars: Optional[int] = Field(None, gt=0, le=2_000_000)   # synthetic test mode: short flat series, no warm-up
    start_time: str = Field("09:00", pattern=r"^\d{2}:\d{2}$")   # New York wall clock of the replay start
    warmup_days: int = Field(3, ge=0, le=10)                  # parquet: history loaded before `start`
    tfs: list[int] = DEFAULT_TFS
    latency_ms: int = Field(150, ge=0, le=10_000)
    slippage_ticks: int = Field(1, ge=0, le=50)


class CmdPlay(BaseModel):
    cmd: Literal["play"]


class CmdPause(BaseModel):
    cmd: Literal["pause"]


class CmdSpeed(BaseModel):
    cmd: Literal["speed"]
    value: float = Field(gt=0, le=100_000)    # replay seconds per real second


class CmdStep(BaseModel):
    cmd: Literal["step"]
    n: int = Field(1, ge=1, le=100_000)


class CmdStepBack(BaseModel):
    cmd: Literal["step_back"]
    n: int = Field(1, ge=1, le=100_000)


class CmdSeek(BaseModel):
    cmd: Literal["seek"]
    ts: int                      # epoch seconds


class CmdOrder(BaseModel):
    cmd: Literal["order"]
    side: Literal["long", "short"]
    qty: int = Field(1, ge=1, le=1000)
    otype: Literal["market", "limit", "stop"] = "market"
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    trail_ticks: Optional[int] = Field(None, ge=1)
    reduce_only: bool = False


class CmdCancel(BaseModel):
    cmd: Literal["cancel"]
    order_id: int


class CmdModify(BaseModel):
    cmd: Literal["modify"]
    position_id: int
    sl: Optional[float] = None
    tp: Optional[float] = None


class CmdClose(BaseModel):
    cmd: Literal["close"]
    position_id: int
    qty: Optional[int] = Field(None, ge=1)


class CmdView(BaseModel):
    """Declare which timeframe (seconds) this client is displaying; the server then sends only that one."""
    cmd: Literal["view"]
    tf: int = Field(gt=0)


Cmd = Annotated[Union[CmdPlay, CmdPause, CmdSpeed, CmdStep, CmdStepBack, CmdSeek, CmdOrder,
                      CmdCancel, CmdModify, CmdClose, CmdView], Field(discriminator="cmd")]
_cmd = TypeAdapter(Cmd)


# --------------------------------------------------------------------------- serializers
def _bar(b: Bar) -> dict:
    return {"t": b.ts // S, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v}


def _finite(x):
    return x if x is not None and math.isfinite(x) else None


def _positions(br: Broker) -> list[dict]:
    return [{"id": p.id, "side": p.side.name.lower(), "qty": p.qty, "entry": p.entry, "sl": p.sl,
             "tp": p.tp, "trail_ticks": p.trail_ticks, "opened": p.opened_ns // S} for p in br.positions.values()]


def _orders(br: Broker) -> list[dict]:
    return [{"id": o.id, "side": o.side.name.lower(), "qty": o.qty, "otype": o.otype.value,
             "price": o.price, "sl": o.sl, "tp": o.tp, "status": o.status.value} for o in br.orders]


def _trade(t) -> dict:
    return {"side": t.side.name.lower(), "qty": t.qty, "entry": t.entry, "exit": t.exit, "pnl": t.pnl,
            "r": t.r_mult, "opened": t.opened_ns // S, "closed": t.closed_ns // S, "reason": t.reason}


# --------------------------------------------------------------------------- session runtime
@dataclass
class Client:
    """One connected browser tab. Bar data is sent per client: only for the timeframe it displays."""
    ws: WebSocket
    tf: int
    sent_ts: int = -1        # open time (ns) of the newest closed bar of `tf` this client already has
    sent_trades: int = 0


class Runtime:
    def __init__(self, sid: str, ss: ReplaySession):
        self.id, self.ss = sid, ss
        self.clients: dict[WebSocket, Client] = {}
        self.task: Optional[asyncio.Task] = None

    # ---- messages
    def account(self) -> dict:
        br = self.ss.broker
        return _clean({"balance": br.balance, "equity": br.equity(), "margin": br.margin_used(),
                       "unrealized": br.unrealized(), "stats": stats(self.ss)})

    def sessions(self) -> dict:
        a = self.ss.asia
        return {"asia": {"hi": _finite(a.high), "lo": _finite(a.low)}}

    def _history(self, c: Client) -> tuple[dict, dict]:
        """History + forming bar for the client's timeframe only; marks them as sent."""
        ss, tf = self.ss, c.tf
        closed = ss.mtf.closed(tf)
        c.sent_ts = closed[-1].ts if closed else -1
        f = ss.mtf.forming(tf)
        return {str(tf): [_bar(b) for b in list(closed)[-HISTORY:]]}, {str(tf): _bar(f) if f else None}

    def _shared(self) -> dict:
        """
        The parts of a tick/reset that are identical for every client. Built ONCE per broadcast: account()
        walks the whole equity curve, so computing it per client multiplied that cost by the tab count, and
        building it once also gives every client the same snapshot.
        """
        ss = self.ss
        last = ss.bars[ss.cursor - 1] if ss.cursor else None
        return {"now": ss.now // S, "cursor": ss.cursor, "total": len(ss.bars), "bar": _bar(last) if last else None,
                "positions": _positions(ss.broker), "orders": _orders(ss.broker),
                "account": self.account(), "sessions": self.sessions()}

    def reset_msg(self, c: Client, sh: Optional[dict] = None) -> dict:
        ss = self.ss
        sh = sh or self._shared()
        hist, forming = self._history(c)
        c.sent_trades = len(ss.broker.trades)
        return {"type": "reset", "tf": c.tf, "now": sh["now"], "cursor": sh["cursor"], "total": sh["total"],
                "state": ss.state.name.lower(), "speed": ss.speed, "history": hist, "forming": forming,
                "positions": sh["positions"], "orders": sh["orders"], "trades": [_trade(t) for t in ss.broker.trades],
                "account": sh["account"], "sessions": sh["sessions"]}

    def view_msg(self, c: Client) -> dict:
        """Sent when a client switches timeframe: just the new timeframe's bars."""
        hist, forming = self._history(c)
        return {"type": "view", "tf": c.tf, "history": hist, "forming": forming}

    def tick_msg(self, c: Client, sh: Optional[dict] = None) -> dict:
        ss, tf = self.ss, c.tf
        sh = sh or self._shared()
        fresh: list[Bar] = []                       # newest first
        for b in reversed(ss.mtf.closed(tf)):
            if b.ts <= c.sent_ts:
                break
            fresh.append(b)
        if fresh:
            c.sent_ts = fresh[0].ts
        new_trades = [_trade(t) for t in ss.broker.trades[c.sent_trades:]]
        c.sent_trades = len(ss.broker.trades)
        f = ss.mtf.forming(tf)
        return {"type": "tick", "tf": tf, **sh,
                "closed": {str(tf): [_bar(b) for b in reversed(fresh)]} if fresh else {},
                "forming": {str(tf): _bar(f) if f else None}, "new_trades": new_trades}

    async def _send(self, c: Client, msg: dict) -> None:
        msg = _clean(msg)
        try:
            await c.ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):      # client went away
            self.clients.pop(c.ws, None)
        except Exception:
            log.exception("failed to send %s message", msg.get("type"))   # a bug, not a disconnect

    async def broadcast(self, msg: dict) -> None:
        """Same message to every client (state changes, acks)."""
        for c in list(self.clients.values()):
            await self._send(c, msg)

    async def broadcast_each(self, build) -> None:
        """Per-client message (bar data differs by the timeframe each client views); shared parts built once."""
        if not self.clients:
            return
        shared = self._shared()
        for c in list(self.clients.values()):
            await self._send(c, build(c, shared))

    # ---- playback
    async def _play(self) -> None:
        ss, acc = self.ss, 0.0
        loop = asyncio.get_running_loop()
        last = loop.time()
        ss.state = Clock.PLAYING
        await self.broadcast({"type": "state", "state": "playing", "speed": ss.speed})
        while ss.state is Clock.PLAYING and ss.cursor < len(ss.bars):
            await asyncio.sleep(FRAME_S)
            nowt = loop.time()
            nxt = ss.bars[ss.cursor]
            acc += (nowt - last) * ss.speed / (nxt.dur_ns / S)
            last = nowt
            n = min(int(acc), MAX_BARS_PER_FRAME, len(ss.bars) - ss.cursor)
            if n <= 0:
                continue
            acc = 0.0 if n == MAX_BARS_PER_FRAME else acc - n
            ss.step(n)
            await self.broadcast_each(self.tick_msg)
        if ss.cursor >= len(ss.bars):
            ss.state = Clock.PAUSED
            await self.broadcast({"type": "end", "account": self.account()})
        await self.broadcast({"type": "state", "state": ss.state.name.lower(), "speed": ss.speed})

    def play(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._play())

    async def pause(self) -> None:
        self.ss.pause()
        if self.task and not self.task.done():
            await self.task
        self.task = None

    async def close(self) -> None:
        await self.pause()
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass

    # ---- commands
    async def handle(self, c, client: Client) -> Optional[dict]:
        ss, br = self.ss, self.ss.broker
        if isinstance(c, CmdView):
            if c.tf not in ss.tfs:
                return {"type": "error", "error": f"unknown timeframe {c.tf}; available: {list(ss.tfs)}"}
            client.tf = c.tf
            await self._send(client, self.view_msg(client))
        elif isinstance(c, CmdPlay):
            self.play()
        elif isinstance(c, CmdPause):
            await self.pause()
        elif isinstance(c, CmdSpeed):
            ss.speed = c.value
            await self.broadcast({"type": "state", "state": ss.state.name.lower(), "speed": ss.speed})
        elif isinstance(c, CmdStep):
            await self.pause()
            ss.step(c.n)
            await self.broadcast_each(self.tick_msg)
        elif isinstance(c, (CmdStepBack, CmdSeek)):
            await self.pause()
            before = ss.cursor
            if isinstance(c, CmdStepBack):
                ss.step_back(c.n)
            else:
                ss.seek(c.ts * S)
            # forward moves are incremental (no chart redraw); backward moves rebuild the client view
            await self.broadcast_each(self.tick_msg if ss.cursor >= before else self.reset_msg)
        elif isinstance(c, CmdOrder):
            if ss.cursor == 0:
                return {"type": "error", "error": "step the replay forward before placing orders"}
            o = Order(Side.LONG if c.side == "long" else Side.SHORT, c.qty, OType(c.otype), c.price,
                      c.sl, c.tp, c.trail_ticks, c.reduce_only)
            ss.submit(o)    # via the session so the action is logged and survives step_back
            await self.broadcast({"type": "order_ack", "order_id": o.id, "status": o.status.value})
            await self.broadcast_each(self.tick_msg)
        elif isinstance(c, CmdCancel):
            ss.cancel(c.order_id)
            await self.broadcast_each(self.tick_msg)
        elif isinstance(c, CmdModify):
            if c.position_id not in br.positions:
                return {"type": "error", "error": f"no position {c.position_id}"}
            ss.modify(c.position_id, c.sl, c.tp)
            await self.broadcast_each(self.tick_msg)
        elif isinstance(c, CmdClose):
            if c.position_id not in br.positions:
                return {"type": "error", "error": f"no position {c.position_id}"}
            ss.close(c.position_id, c.qty)
            await self.broadcast_each(self.tick_msg)
        return None


# --------------------------------------------------------------------------- app
app = FastAPI(title="NQ Replay")
app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS), allow_methods=["*"], allow_headers=["*"])
RUNTIMES: dict[str, Runtime] = {}


def _build(req: CreateSession) -> ReplaySession:
    inst = NQ if req.instrument == "NQ" else MNQ
    xm = ExecModel(latency_ms=req.latency_ms, slippage_ticks_market=req.slippage_ticks,
                   slippage_ticks_stop=req.slippage_ticks)
    warm = 0
    if req.source == "synthetic":
        if req.bars:                                  # test/demo mode: short series from bar 0
            bars = synthetic_bars(req.bars)
        else:
            bars = synthetic_days()
            warm = start_index(bars, date(2025, 3, 10), req.start_time)
    else:
        if not (req.start and req.end):
            raise HTTPException(422, "start and end are required for parquet sessions")
        if req.end < req.start:
            raise HTTPException(422, "end is before start")
        import nq_data
        try:
            bars = nq_data.load_bars(req.symbol, req.start - timedelta(days=req.warmup_days * 7 // 5 + 3),
                                     req.end, back_adjust=req.back_adjust)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        warm = start_index(bars, req.start, req.start_time)
        if warm >= len(bars):
            raise HTTPException(404, f"no data at or after {req.start} {req.start_time} NY")
    ss = ReplaySession(bars, inst, tfs=tuple(req.tfs), exec_model=xm, balance=req.balance)
    if warm:
        ss.preload(warm)
    return ss


@app.post("/sessions")
def create_session(req: CreateSession) -> dict:
    if len(RUNTIMES) >= MAX_SESSIONS:
        raise HTTPException(429, "too many sessions")
    sid = uuid.uuid4().hex[:12]
    ss = _build(req)
    ss.speed = 60.0       # initial only (replay-seconds per real second); the UI sends its own on connect
    RUNTIMES[sid] = Runtime(sid, ss)
    return {"id": sid, "bars": len(ss.bars), "first": ss.bars[0].ts // S, "last": ss.bars[-1].ts // S,
            "start_cursor": ss.cursor}


@app.get("/sessions/{sid}")
def get_session(sid: str) -> dict:
    rt = RUNTIMES.get(sid)
    if not rt:
        raise HTTPException(404, "unknown session")
    return {"id": sid, "cursor": rt.ss.cursor, "total": len(rt.ss.bars), "state": rt.ss.state.name.lower(),
            "account": rt.account()}


@app.delete("/sessions/{sid}")
async def delete_session(sid: str) -> dict:
    rt = RUNTIMES.pop(sid, None)
    if not rt:
        raise HTTPException(404, "unknown session")
    await rt.close()
    return {"deleted": sid}


@app.websocket("/ws/{sid}")
async def ws(websocket: WebSocket, sid: str, tf: Optional[int] = None) -> None:
    """
    `?tf=<seconds>` selects the timeframe whose bars this client receives (change later with the `view`
    command). Omitted: 1m if the session has it, otherwise its first timeframe.
    """
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    rt = RUNTIMES.get(sid)
    if not rt:
        await websocket.close(code=4404)
        return
    if tf is None:
        tf = 60 if 60 in rt.ss.tfs else rt.ss.tfs[0]
    if tf not in rt.ss.tfs:
        await websocket.close(code=4400)       # unknown timeframe
        return
    await websocket.accept()
    client = Client(websocket, tf)
    rt.clients[websocket] = client
    try:
        await websocket.send_json(_clean(rt.reset_msg(client)))
        while True:
            raw = await websocket.receive_json()
            try:
                c = _cmd.validate_python(raw)
            except ValidationError as e:
                await websocket.send_json({"type": "error", "error": e.errors(include_url=False)[0]["msg"]})
                continue
            err = await rt.handle(c, client)
            if err:
                await websocket.send_json(err)
    except WebSocketDisconnect:
        pass
    finally:
        rt.clients.pop(websocket, None)
        if not rt.clients:
            await rt.pause()   # nobody watching: don't let playback run away
