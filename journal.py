"""
journal.py - local trade journal (SQLite) for replay sessions.

Every closed trade of a replay session is mirrored into `data/journal.sqlite` (override with the
NQ_JOURNAL environment variable) so it can be annotated, analysed and replayed later. Nothing here
talks to the network; the file stays on this machine.

How trades get in
  `Journal.sync(session_id, trades)` is called as a session runs. Trades are identified by a content
  key (session, open/close time, side, qty, prices + an ordinal for exact duplicates), so replaying the
  same moves after a step back produces the same keys and a note attached to a trade stays attached.
  After a step back, trades that no longer exist in the replay are deleted, unless they carry notes:
  those are kept but hidden (`live = 0`) and come back if the same trade is replayed again.
  Trades the user deletes are tombstoned (`live = 2`) so a later sync cannot bring them back.

MAE/MFE are bar-level, in points: the worst/best excursion against/for the entry using each bar's
high/low while the position was open (the order of high and low inside a bar is unknown).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
S = 10**9
DEFAULT_PATH = "data/journal.sqlite"
MAX_TEXT = 5000
MAX_TAG = 40
MAX_MISTAKES = 10
NOTE_FIELDS = ("setup", "emotion", "rating", "mistakes", "note")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created INTEGER NOT NULL,
    meta TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    point_value REAL NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry REAL NOT NULL,
    exit REAL NOT NULL,
    pnl REAL NOT NULL,
    r REAL,
    opened INTEGER NOT NULL,
    closed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    mae REAL NOT NULL DEFAULT 0,
    mfe REAL NOT NULL DEFAULT 0,
    day TEXT NOT NULL,
    live INTEGER NOT NULL DEFAULT 1,
    setup TEXT,
    emotion TEXT,
    rating INTEGER,
    mistakes TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS trades_day ON trades(day);
CREATE INDEX IF NOT EXISTS trades_session ON trades(session_id);
"""


def trading_day(ts_s: int) -> str:
    """CME trading day (ISO date) of an instant: the session opens 18:00 New York the evening before."""
    local = datetime.fromtimestamp(ts_s, NY)
    d = local.date() + timedelta(days=1 if local.hour >= 18 else 0)
    if d.weekday() == 5:  # Saturday -> Monday
        d += timedelta(days=2)
    elif d.weekday() == 6:  # Sunday -> Monday
        d += timedelta(days=1)
    return d.isoformat()


def clean_notes(raw: dict) -> dict:
    """Validate/normalise the user-editable fields. Raises ValueError on bad input."""
    out: dict = {}
    for k in raw:
        if k not in NOTE_FIELDS:
            raise ValueError(f"unknown field {k!r}")
    for k in ("setup", "emotion"):
        if k in raw:
            v = raw[k]
            if v is not None:
                if not isinstance(v, str):
                    raise ValueError(f"{k} must be text")
                v = v.strip()
                if len(v) > MAX_TAG:
                    raise ValueError(f"{k} is longer than {MAX_TAG} characters")
            out[k] = v or None
    if "rating" in raw:
        v = raw["rating"]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5):
            raise ValueError("rating must be 1 to 5")
        out["rating"] = v
    if "mistakes" in raw:
        v = raw["mistakes"]
        if not isinstance(v, list) or len(v) > MAX_MISTAKES:
            raise ValueError(f"mistakes must be a list of at most {MAX_MISTAKES} tags")
        tags: list[str] = []
        for t in v:
            if not isinstance(t, str):
                raise ValueError("mistake tags must be text")
            t = t.strip()
            if len(t) > MAX_TAG:
                raise ValueError(f"a mistake tag is longer than {MAX_TAG} characters")
            if t and t not in tags:
                tags.append(t)
        out["mistakes"] = json.dumps(tags)
    if "note" in raw:
        v = raw["note"]
        if not isinstance(v, str) or len(v) > MAX_TEXT:
            raise ValueError(f"note must be text of at most {MAX_TEXT} characters")
        out["note"] = v
    return out


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["mistakes"] = json.loads(d["mistakes"])
    d["live"] = bool(d["live"])
    pv, q = d["point_value"], d["qty"]
    d["mae_usd"], d["mfe_usd"] = d["mae"] * pv * q, d["mfe"] * pv * q
    return d


def _bucket(rows: list[dict], keyfn) -> list[dict]:
    g: dict = defaultdict(list)
    for r in rows:
        ks = keyfn(r)
        for k in ks if isinstance(ks, list) else [ks]:
            g[k].append(r)
    out = []
    for k, rs in g.items():
        rr = [r["r"] for r in rs if r["r"] is not None]
        wins = sum(1 for r in rs if r["pnl"] > 0)
        out.append(
            {
                "key": k,
                "trades": len(rs),
                "wins": wins,
                "win_rate": wins / len(rs),
                "pnl": sum(r["pnl"] for r in rs),
                "avg_r": sum(rr) / len(rr) if rr else None,
            }
        )
    return out


class _Cache:
    """What was last written for a session, so playback only appends new trades."""

    __slots__ = ("counts", "keys")

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.counts: dict[str, int] = {}


class Journal:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, _Cache] = {}
        with closing(self._conn()) as c, c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ---------------------------------------------------------------- writing
    @staticmethod
    def _base_key(sid: str, t) -> str:
        return f"{sid}:{t.opened_ns}:{t.closed_ns}:{t.side.name}:{t.qty}:{t.entry}:{t.exit}"

    def invalidate(self, sid: str) -> None:
        """The replay moved backwards: the next sync rewrites the whole session."""
        with self._lock:
            self._cache.pop(sid, None)

    def sync(self, sid: str, trades: list, meta: dict | None = None) -> int:
        """Mirror a session's closed trades. Returns the number of rows written. `meta` needs
        instrument, point_value (and optionally anything else worth keeping, e.g. the create request)."""
        meta = meta or {}
        inst, pv = meta.get("instrument", "NQ"), float(meta.get("point_value", 20.0))
        with self._lock:
            cache = self._cache.get(sid)
            n = len(trades)
            appendable = (
                cache is not None
                and 0 < len(cache.keys) <= n
                and cache.keys[-1].startswith(
                    self._base_key(sid, trades[len(cache.keys) - 1]) + "#"
                )
            )
            if cache is not None and not appendable and not (n == 0 and not cache.keys):
                cache = None
            full = cache is None
            if full:
                cache = _Cache()
            assert cache is not None
            start = 0 if full else len(cache.keys)
            if not full and start == n:
                return 0
            new_keys: list[str] = []
            rows = []
            for t in trades[start:]:
                base = self._base_key(sid, t)
                ordinal = cache.counts.get(base, 0)
                cache.counts[base] = ordinal + 1
                key = f"{base}#{ordinal}"
                new_keys.append(key)
                opened, closed = t.opened_ns // S, t.closed_ns // S
                rows.append(
                    (
                        key, sid, inst, pv, t.side.name.lower(), t.qty, t.entry, t.exit, t.pnl,
                        t.r_mult, opened, closed, t.reason, t.mae, t.mfe, trading_day(closed),
                    )
                )  # fmt: skip
            with closing(self._conn()) as c, c:
                if meta and full:
                    c.execute(
                        "INSERT INTO sessions(id, created, meta) VALUES(?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET meta=excluded.meta",
                        (sid, int(datetime.now().timestamp()), json.dumps(meta)),
                    )
                existing = {
                    r["key"]: r
                    for r in c.execute("SELECT * FROM trades WHERE session_id=?", (sid,))
                }  # tombstoned (live=2) rows included, so they are never inserted again
                fresh = [r for r in rows if r[0] not in existing]
                revive = [
                    (existing[r[0]]["id"],)
                    for r in rows
                    if r[0] in existing and existing[r[0]]["live"] == 0
                ]
                c.executemany(
                    "INSERT INTO trades(key, session_id, instrument, point_value, side, qty, entry, exit,"
                    " pnl, r, opened, closed, reason, mae, mfe, day) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    fresh,
                )
                c.executemany("UPDATE trades SET live=1 WHERE id=?", revive)
                written = len(fresh) + len(revive)
                if full:  # drop or hide whatever the replay no longer contains
                    keep = set(new_keys)
                    for key, r in existing.items():
                        if r["live"] != 1 or key in keep:
                            continue
                        if (
                            r["setup"]
                            or r["emotion"]
                            or r["rating"]
                            or r["note"]
                            or r["mistakes"] != "[]"
                        ):
                            c.execute("UPDATE trades SET live=0 WHERE id=?", (r["id"],))
                        else:
                            c.execute("DELETE FROM trades WHERE id=?", (r["id"],))
            cache.keys.extend(new_keys)
            self._cache[sid] = cache
            return written

    def update_notes(self, tid: int, raw: dict) -> dict | None:
        fields = clean_notes(raw)
        with closing(self._conn()) as c, c:
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                c.execute(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), tid))  # noqa: S608 - keys are whitelisted
            r = c.execute("SELECT * FROM trades WHERE id=? AND live != 2", (tid,)).fetchone()
        return _row(r) if r else None

    def delete_trade(self, tid: int) -> bool:
        with closing(self._conn()) as c, c:
            row = c.execute("SELECT id FROM trades WHERE id=? AND live != 2", (tid,)).fetchone()
            if not row:
                return False
            c.execute(
                "UPDATE trades SET live=2 WHERE id=?", (tid,)
            )  # tombstone: a later sync must not revive it
        return True

    # ---------------------------------------------------------------- reading
    def get_trade(self, tid: int) -> dict | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM trades WHERE id=? AND live != 2", (tid,)).fetchone()
        return _row(r) if r else None

    def session_meta(self, sid: str) -> dict | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT meta FROM sessions WHERE id=?", (sid,)).fetchone()
        return json.loads(r["meta"]) if r else None

    @staticmethod
    def _where(f: dict) -> tuple[str, list]:
        w, a = ["live=1"], []
        if f.get("session"):
            w.append("session_id=?")
            a.append(f["session"])
        if f.get("start"):
            w.append("day>=?")
            a.append(f["start"])
        if f.get("end"):
            w.append("day<=?")
            a.append(f["end"])
        if f.get("setup"):
            w.append("setup=?")
            a.append(f["setup"])
        if f.get("side"):
            w.append("side=?")
            a.append(f["side"])
        return " AND ".join(w), a

    def trades(self, f: dict | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
        where, args = self._where(f or {})
        with closing(self._conn()) as c:
            rs = c.execute(
                f"SELECT * FROM trades WHERE {where} ORDER BY closed DESC, id DESC LIMIT ? OFFSET ?",  # noqa: S608
                (*args, limit, offset),
            ).fetchall()
        return [_row(r) for r in rs]

    def _all(self, f: dict) -> list[dict]:
        where, args = self._where(f)
        with closing(self._conn()) as c:
            rs = c.execute(
                f"SELECT * FROM trades WHERE {where} ORDER BY closed, id", args
            ).fetchall()  # noqa: S608
        return [_row(r) for r in rs]

    def tags(self) -> dict:
        """Distinct values already used, for autocomplete."""
        out: dict = {"setups": [], "emotions": [], "mistakes": []}
        with closing(self._conn()) as c:
            out["setups"] = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT setup FROM trades WHERE setup IS NOT NULL ORDER BY 1"
                )
            ]
            out["emotions"] = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT emotion FROM trades WHERE emotion IS NOT NULL ORDER BY 1"
                )
            ]
            ms: set[str] = set()
            for (m,) in c.execute("SELECT mistakes FROM trades WHERE mistakes != '[]'"):
                ms.update(json.loads(m))
        out["mistakes"] = sorted(ms)
        return out

    def analytics(self, f: dict | None = None) -> dict:
        rows = self._all(f or {})
        if not rows:
            return {"summary": {"trades": 0}, "equity": [], "breakdowns": {}}
        wins = [r for r in rows if r["pnl"] > 0]
        losses = [r for r in rows if r["pnl"] <= 0]
        gp, gl = sum(r["pnl"] for r in wins), -sum(r["pnl"] for r in losses)
        rs = [r["r"] for r in rows if r["r"] is not None]
        cum = peak = mdd = 0.0
        equity = []
        for r in rows:
            cum += r["pnl"]
            peak = max(peak, cum)
            mdd = max(mdd, peak - cum)
            equity.append({"t": r["closed"], "pnl": r["pnl"], "cum": cum})
        n = len(rows)
        summary = {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / n,
            "net_pnl": cum,
            "gross_profit": gp,
            "gross_loss": gl,
            "profit_factor": gp / gl if gl else None,
            "avg_win": gp / len(wins) if wins else None,
            "avg_loss": -gl / len(losses) if losses else None,
            "expectancy": cum / n,
            "avg_r": sum(rs) / len(rs) if rs else None,
            "best": max(r["pnl"] for r in rows),
            "worst": min(r["pnl"] for r in rows),
            "max_drawdown": mdd,
            "avg_hold_s": sum(r["closed"] - r["opened"] for r in rows) / n,
            "avg_mae_usd": sum(r["mae_usd"] for r in rows) / n,
            "avg_mfe_usd": sum(r["mfe_usd"] for r in rows) / n,
        }

        def hour(r):
            return datetime.fromtimestamp(r["opened"], NY).hour

        def weekday(r):
            return date.fromisoformat(r["day"]).weekday()

        def srt(items, key="key"):
            return sorted(items, key=lambda x: (x[key] is None, x[key]))

        return {
            "summary": summary,
            "equity": equity[-2000:],
            "breakdowns": {
                "hour": srt(_bucket(rows, hour)),
                "weekday": srt(_bucket(rows, weekday)),
                "side": srt(_bucket(rows, lambda r: r["side"])),
                "setup": sorted(_bucket(rows, lambda r: r["setup"] or "(none)"), key=lambda x: -x["pnl"]),
                "emotion": sorted(_bucket(rows, lambda r: r["emotion"] or "(none)"), key=lambda x: -x["pnl"]),
                "mistake": sorted(_bucket([r for r in rows if r["mistakes"]], lambda r: r["mistakes"]), key=lambda x: x["pnl"]),
                "reason": srt(_bucket(rows, lambda r: r["reason"])),
            },
        }  # fmt: skip

    def calendar(self, year: int, month: int) -> dict:
        first = date(year, month, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        rows = self._all({"start": first.isoformat(), "end": last.isoformat()})
        days: dict[str, dict] = {}
        for r in rows:
            d = days.setdefault(r["day"], {"day": r["day"], "trades": 0, "wins": 0, "pnl": 0.0})
            d["trades"] += 1
            d["wins"] += r["pnl"] > 0
            d["pnl"] += r["pnl"]
        total = sum(d["pnl"] for d in days.values())
        return {
            "year": year,
            "month": month,
            "days": [days[k] for k in sorted(days)],
            "net_pnl": total,
            "trades": sum(d["trades"] for d in days.values()),
            "green_days": sum(1 for d in days.values() if d["pnl"] > 0),
            "red_days": sum(1 for d in days.values() if d["pnl"] < 0),
        }


# ---------------------------------------------------------------- shared instance
_instance: Journal | None = None
_instance_lock = threading.Lock()


def get() -> Journal:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Journal(os.environ.get("NQ_JOURNAL", DEFAULT_PATH))
        return _instance


def configure(path: str | os.PathLike | None) -> None:
    """Point the shared journal somewhere else (tests); None forgets it so the next get() reopens."""
    global _instance
    with _instance_lock:
        _instance = Journal(path) if path else None
