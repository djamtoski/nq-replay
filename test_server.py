import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server

S = 10**9


@pytest.fixture()
def client():
    server.RUNTIMES.clear()
    with TestClient(server.app) as c:
        yield c
    server.RUNTIMES.clear()


def new_session(client, **kw):
    body = {"bars": 1200, "latency_ms": 0, "slippage_ticks": 0, "tfs": [60, 300], **kw}
    r = client.post("/sessions", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def recv_until(ws, typ, limit=200):
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == typ:
            return m
    raise AssertionError(f"no {typ} message")


def test_create_get_delete(client):
    sid = new_session(client)
    assert client.get(f"/sessions/{sid}").json()["cursor"] == 0
    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert client.get(f"/sessions/{sid}").status_code == 404


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "NQ..v", "", "x" * 40])
def test_parquet_symbol_cannot_escape_the_data_directory(client, bad):
    r = client.post(
        "/sessions",
        json={"source": "parquet", "symbol": bad, "start": "2025-03-10", "end": "2025-03-11"},
    )
    assert r.status_code == 422


def test_create_validation(client):
    assert client.post("/sessions", json={"balance": -5}).status_code == 422
    assert client.post("/sessions", json={"source": "parquet"}).status_code == 422
    assert (
        client.post(
            "/sessions", json={"source": "parquet", "start": "2001-01-01", "end": "2001-01-02"}
        ).status_code
        == 404
    )


def test_hello_reset_then_step(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        r = ws.receive_json()
        assert r["type"] == "reset" and r["cursor"] == 0 and r["history"]["60"] == []
        ws.send_json({"cmd": "step", "n": 130})
        t = ws.receive_json()
        assert t["type"] == "tick" and t["cursor"] == 130
        assert len(t["closed"]["60"]) == 2  # minutes 0 and 1 closed; minute 2 still forming
        assert t["forming"]["60"] is not None
        assert t["bar"]["t"] == t["now"] - 1


def test_never_sends_future_bars(client):
    sid = new_session(client)
    rt = server.RUNTIMES[sid]
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 100})
        t = ws.receive_json()
        cutoff = rt.ss.bars[99].ts // S
        for bars in t["closed"].values():
            assert all(b["t"] <= cutoff for b in bars)
        assert all(f["t"] <= cutoff for f in t["forming"].values() if f)


def test_order_flow_fill_and_close(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 5})
        ws.receive_json()
        ws.send_json({"cmd": "order", "side": "long", "qty": 1, "otype": "market"})
        ack = recv_until(ws, "order_ack")
        assert ack["status"] == "pending"
        recv_until(ws, "tick")
        ws.send_json({"cmd": "step", "n": 2})
        t = recv_until(ws, "tick")
        assert len(t["positions"]) == 1
        pid = t["positions"][0]["id"]
        ws.send_json({"cmd": "modify", "position_id": pid, "sl": 1.0, "tp": 99999.0})
        t = recv_until(ws, "tick")
        assert t["positions"][0]["sl"] == 1.0
        ws.send_json({"cmd": "close", "position_id": pid})
        recv_until(ws, "tick")
        ws.send_json({"cmd": "step", "n": 2})
        t = recv_until(ws, "tick")
        assert t["positions"] == [] and len(t["new_trades"]) == 1
        assert t["account"]["stats"]["trades"] == 1


def test_order_before_start_rejected(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "order", "side": "long"})
        assert ws.receive_json()["type"] == "error"


def test_bad_commands_return_errors_not_disconnects(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        for bad in (
            {"cmd": "nope"},
            {"cmd": "speed", "value": -1},
            {"cmd": "step", "n": 0},
            {"cmd": "modify", "position_id": 999},
            {"cmd": "close", "position_id": 999},
        ):
            ws.send_json(bad)
            assert ws.receive_json()["type"] == "error"
        ws.send_json({"cmd": "step", "n": 1})
        assert ws.receive_json()["type"] == "tick"


def test_step_back_and_seek_send_reset_and_match_forward_state(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 400})
        fwd = ws.receive_json()
        ws.send_json({"cmd": "step_back", "n": 100})
        r = ws.receive_json()
        assert r["type"] == "reset" and r["cursor"] == 300
        ws.send_json({"cmd": "step", "n": 100})
        again = ws.receive_json()
        assert again["cursor"] == 400 and again["bar"] == fwd["bar"]
        ws.send_json({"cmd": "seek", "ts": fwd["now"] - 200})
        r = ws.receive_json()
        assert r["type"] == "reset" and r["cursor"] == 200


def test_manual_order_survives_step_back_over_ws(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        ws.receive_json()
        ws.send_json({"cmd": "order", "side": "long", "qty": 1, "otype": "market"})
        recv_until(ws, "order_ack")
        recv_until(ws, "tick")
        ws.send_json({"cmd": "step", "n": 5})
        t = recv_until(ws, "tick")
        pid = t["positions"][0]["id"]
        ws.send_json({"cmd": "step_back", "n": 2})
        r = recv_until(ws, "reset")
        assert r["cursor"] == 13 and [p["id"] for p in r["positions"]] == [pid]
        ws.send_json({"cmd": "close", "position_id": pid})  # id still valid after replay
        recv_until(ws, "tick")
        ws.send_json({"cmd": "step", "n": 2})
        assert recv_until(ws, "tick")["positions"] == []


def test_default_session_has_warmup_history_on_every_timeframe(client):
    r = client.post("/sessions", json={}).json()
    assert r["start_cursor"] > 100_000  # days of history already revealed
    for tf in (
        "1",
        "15",
        "60",
        "300",
        "900",
        "3600",
        "14400",
    ):  # every timeframe has history to show
        with client.websocket_connect(f"/ws/{r['id']}?tf={tf}") as ws:
            m = ws.receive_json()
            assert list(m["history"]) == [tf] and len(m["history"][tf]) >= 15, (
                tf
            )  # empty HTF charts = bug
            assert m["sessions"]["asia"]["hi"] is not None  # Asian range already known at 09:00 NY
    with client.websocket_connect(f"/ws/{r['id']}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step_back", "n": 10_000})  # can't rewind into the warm-up
        assert ws.receive_json()["cursor"] == r["start_cursor"]


def test_only_the_viewed_timeframe_is_sent(client):
    sid = new_session(client, tfs=[1, 15, 60, 300])
    with client.websocket_connect(f"/ws/{sid}?tf=300") as ws:
        r = ws.receive_json()
        assert r["tf"] == 300 and list(r["history"]) == ["300"] and list(r["forming"]) == ["300"]
        ws.send_json({"cmd": "step", "n": 700})
        t = ws.receive_json()
        assert t["tf"] == 300 and list(t["forming"]) == ["300"]
        assert (
            list(t["closed"]) == ["300"] and len(t["closed"]["300"]) == 2
        )  # only 5m bars, 2 closed in 700s


def test_payload_shrinks_when_viewing_a_large_timeframe(client):
    sid = new_session(client, tfs=[1, 15, 60, 300, 900], bars=3000)
    sizes = {}
    for tf in (1, 900):
        with client.websocket_connect(f"/ws/{sid}?tf={tf}") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "step", "n": 900})
            sizes[tf] = len(json.dumps(ws.receive_json()))
            ws.send_json({"cmd": "step_back", "n": 900})
            ws.receive_json()
    assert sizes[900] < sizes[1] / 5  # 1s view ships 900 new bars; 15m view ships 1


def test_clients_view_different_timeframes_independently(client):
    sid = new_session(client, tfs=[60, 300])
    with (
        client.websocket_connect(f"/ws/{sid}?tf=60") as a,
        client.websocket_connect(f"/ws/{sid}?tf=300") as b,
    ):
        a.receive_json()
        b.receive_json()
        a.send_json({"cmd": "step", "n": 400})
        ta, tb = (
            a.receive_json(),
            b.receive_json(),
        )  # both get the broadcast tick, each for its own tf
        assert list(ta["closed"]) == ["60"] and len(ta["closed"]["60"]) == 6
        assert list(tb["closed"]) == ["300"] and len(tb["closed"]["300"]) == 1
        assert ta["cursor"] == tb["cursor"] == 400


def test_view_command_switches_timeframe_and_streams_new_one(client):
    sid = new_session(client, tfs=[60, 300])
    with client.websocket_connect(f"/ws/{sid}?tf=60") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 650})
        assert list(ws.receive_json()["closed"]) == ["60"]
        ws.send_json({"cmd": "view", "tf": 300})
        v = ws.receive_json()
        assert (
            v["type"] == "view"
            and v["tf"] == 300
            and len(v["history"]["300"]) == 2
            and v["forming"]["300"]
        )
        ws.send_json({"cmd": "step", "n": 300})
        t = ws.receive_json()
        assert (
            list(t["closed"]) == ["300"] and len(t["closed"]["300"]) == 1
        )  # no duplicates of what view sent
        assert t["closed"]["300"][0]["t"] > v["history"]["300"][-1]["t"]


def test_session_without_a_60s_timeframe_still_accepts_clients_that_omit_tf(client):
    sid = new_session(client, tfs=[15, 300])
    with client.websocket_connect(f"/ws/{sid}") as ws:  # no ?tf=: falls back to the first timeframe
        r = ws.receive_json()
        assert r["tf"] == 15 and list(r["history"]) == ["15"]
    sid2 = new_session(client, tfs=[1, 60, 300])
    with client.websocket_connect(f"/ws/{sid2}") as ws:  # 1m present: it stays the default
        assert ws.receive_json()["tf"] == 60


def test_shared_tick_parts_are_built_once_per_broadcast_not_per_client(client, monkeypatch):
    sid = new_session(client, tfs=[60, 300])
    rt = server.RUNTIMES[sid]
    calls = {"n": 0}
    real = rt.account
    monkeypatch.setattr(rt, "account", lambda: (calls.__setitem__("n", calls["n"] + 1), real())[1])
    with (
        client.websocket_connect(f"/ws/{sid}?tf=60") as a,
        client.websocket_connect(f"/ws/{sid}?tf=300") as b,
    ):
        a.receive_json()
        b.receive_json()
        calls["n"] = 0
        a.send_json({"cmd": "step", "n": 10})
        ta, tb = a.receive_json(), b.receive_json()
        assert calls["n"] == 1  # account()/stats() once for two tabs
        assert (
            ta["account"] == tb["account"]
            and ta["positions"] == tb["positions"]
            and ta["now"] == tb["now"]
        )


def test_switching_timeframe_during_playback_has_no_gaps_or_duplicates(client):
    sid = new_session(client, tfs=[60, 300], bars=20000)
    with client.websocket_connect(f"/ws/{sid}?tf=60") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "speed", "value": 3000})  # a 5m bar every ~0.1 s
        recv_until(ws, "state")
        ws.send_json({"cmd": "play"})
        while recv_until(ws, "tick")["cursor"] < 1000:  # let a few 5m bars close first
            pass
        ws.send_json({"cmd": "view", "tf": 300})  # switch while ticks for 1m are still arriving
        v = recv_until(ws, "view")  # (stale 1m ticks before it are expected)
        last = v["history"]["300"][-1]["t"]
        seen = []
        while len(seen) < 6:
            m = ws.receive_json()
            if m["type"] != "tick":
                continue
            assert m["tf"] == 300 and set(m["closed"]) <= {
                "300"
            }  # nothing for the old timeframe after the switch
            seen += [b["t"] for b in m["closed"].get("300", [])]
        assert seen == [
            last + 300 * (i + 1) for i in range(len(seen))
        ]  # contiguous: no gap, no repeat
        ws.send_json({"cmd": "pause"})


def test_view_rejects_unknown_timeframe_and_ws_rejects_bad_query(client):
    sid = new_session(client, tfs=[60, 300])
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "view", "tf": 7})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"cmd": "view", "tf": 0})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"cmd": "step", "n": 1})
        assert ws.receive_json()["tf"] == 60  # still on the original timeframe
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/{sid}?tf=7"):
            pass


def test_new_trades_are_tracked_per_client(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as a:
        a.receive_json()
        a.send_json({"cmd": "step", "n": 5})
        a.receive_json()
        px = server.RUNTIMES[sid].ss.bars[4].c
        a.send_json({"cmd": "order", "side": "long", "qty": 1, "otype": "market", "tp": px + 0.25})
        recv_until(a, "tick")
        a.send_json({"cmd": "step", "n": 400})
        assert len(recv_until(a, "tick")["new_trades"]) == 1
        with client.websocket_connect(
            f"/ws/{sid}"
        ) as late:  # a late joiner gets history in `reset`, not re-sent as new
            r = late.receive_json()
            assert len(r["trades"]) == 1
            late.send_json({"cmd": "step", "n": 10})
            assert late.receive_json()["new_trades"] == []


def test_messages_are_strict_json_after_all_winning_trades(client):
    """Regression: profit_factor=inf was serialised as `Infinity`, which JSON.parse in browsers rejects,
    freezing the UI after the first winning streak."""
    import json

    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 5})
        ws.receive_json()
        px = server.RUNTIMES[sid].ss.bars[4].c
        ws.send_json({"cmd": "order", "side": "long", "qty": 1, "otype": "market", "tp": px + 0.25})
        recv_until(ws, "tick")
        ws.send_json({"cmd": "step", "n": 400})
        t = recv_until(ws, "tick")
        assert (
            t["account"]["stats"]["trades"] >= 1 and t["account"]["stats"]["profit_factor"] is None
        )
    assert server._clean({"a": float("inf"), "b": [float("nan"), 1.5]}) == {
        "a": None,
        "b": [None, 1.5],
    }
    json.dumps(server._clean({"x": float("inf")}), allow_nan=False)


def test_seek_forward_is_incremental_and_backward_is_reset(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        t0 = server.RUNTIMES[sid].ss.bars[0].ts // S
        ws.send_json({"cmd": "seek", "ts": t0 + 300})  # forward: tick, no chart rebuild
        t = ws.receive_json()
        assert t["type"] == "tick" and t["cursor"] == 300 and len(t["closed"]["60"]) == 4
        ws.send_json({"cmd": "seek", "ts": t0 + 120})
        assert ws.receive_json()["type"] == "reset"


def test_playback_runs_to_end_at_high_speed(client):
    sid = new_session(client, bars=300)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "speed", "value": 1000})
        recv_until(ws, "state")
        ws.send_json({"cmd": "play"})
        end = recv_until(ws, "end", limit=500)
        assert "equity" in end["account"]
        assert client.get(f"/sessions/{sid}").json()["cursor"] == 300


def test_pause_stops_playback(client):
    sid = new_session(client, bars=5000)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "speed", "value": 20})
        recv_until(ws, "state")
        ws.send_json({"cmd": "play"})
        recv_until(ws, "tick")
        ws.send_json({"cmd": "pause"})
        c1 = None
        for _ in range(100):
            m = ws.receive_json()
            if m["type"] == "state" and m["state"] == "paused":
                break
        c1 = client.get(f"/sessions/{sid}").json()["cursor"]
        c2 = client.get(f"/sessions/{sid}").json()["cursor"]
        assert c1 == c2 and c1 < 5000


def test_unknown_session_and_bad_origin_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/nope"):
            pass
    sid = new_session(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/{sid}", headers={"origin": "http://evil.example"}):
            pass
    with client.websocket_connect(f"/ws/{sid}", headers={"origin": "http://localhost:5173"}) as ws:
        assert ws.receive_json()["type"] == "reset"


def test_disconnect_pauses_playback(client):
    sid = new_session(client, bars=5000)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "speed", "value": 50})
        recv_until(ws, "state")
        ws.send_json({"cmd": "play"})
        recv_until(ws, "tick")
    assert server.RUNTIMES[sid].ss.state.name == "PAUSED"
