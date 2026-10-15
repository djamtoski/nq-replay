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


def test_create_validation(client):
    assert client.post("/sessions", json={"balance": -5}).status_code == 422
    assert client.post("/sessions", json={"source": "parquet"}).status_code == 422
    assert client.post("/sessions", json={"source": "parquet", "start": "2001-01-01", "end": "2001-01-02"}
                       ).status_code == 404


def test_hello_reset_then_step(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        r = ws.receive_json()
        assert r["type"] == "reset" and r["cursor"] == 0 and r["history"]["60"] == []
        ws.send_json({"cmd": "step", "n": 130})
        t = ws.receive_json()
        assert t["type"] == "tick" and t["cursor"] == 130
        assert len(t["closed"]["60"]) == 2           # minutes 0 and 1 closed; minute 2 still forming
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
        for tf, bars in t["closed"].items():
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
        for bad in ({"cmd": "nope"}, {"cmd": "speed", "value": -1}, {"cmd": "step", "n": 0},
                    {"cmd": "modify", "position_id": 999}, {"cmd": "close", "position_id": 999}):
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
        ws.send_json({"cmd": "close", "position_id": pid})      # id still valid after replay
        recv_until(ws, "tick")
        ws.send_json({"cmd": "step", "n": 2})
        assert recv_until(ws, "tick")["positions"] == []


def test_default_session_has_warmup_history_on_every_timeframe(client):
    r = client.post("/sessions", json={}).json()
    assert r["start_cursor"] > 100_000                       # days of history already revealed
    with client.websocket_connect(f"/ws/{r['id']}") as ws:
        m = ws.receive_json()
        for tf in ("1", "15", "60", "300", "900", "3600", "14400"):
            assert len(m["history"][tf]) >= 15, tf           # no empty higher-timeframe charts
        assert m["sessions"]["asia"]["hi"] is not None       # Asian range already known at 09:00 NY
        ws.send_json({"cmd": "step_back", "n": 10_000})      # can't rewind into the warm-up
        assert ws.receive_json()["cursor"] == r["start_cursor"]


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
        assert t["account"]["stats"]["trades"] >= 1 and t["account"]["stats"]["profit_factor"] is None
    assert server._clean({"a": float("inf"), "b": [float("nan"), 1.5]}) == {"a": None, "b": [None, 1.5]}
    json.dumps(server._clean({"x": float("inf")}), allow_nan=False)


def test_seek_forward_is_incremental_and_backward_is_reset(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        t0 = server.RUNTIMES[sid].ss.bars[0].ts // S
        ws.send_json({"cmd": "seek", "ts": t0 + 300})          # forward: tick, no chart rebuild
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
