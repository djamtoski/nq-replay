"""Journal behaviour through the real server: trades recorded while a session runs, the REST API."""

import pytest
from fastapi.testclient import TestClient

import journal
import server
from test_server import new_session, recv_until


@pytest.fixture()
def client():
    server.RUNTIMES.clear()
    with TestClient(server.app) as c:
        yield c
    server.RUNTIMES.clear()


def trade_once(client, ws, side="long"):
    """Open and close one position over the websocket; returns after the close has filled."""
    ws.send_json({"cmd": "order", "side": side, "qty": 1, "otype": "market"})
    recv_until(ws, "order_ack")
    recv_until(ws, "tick")
    ws.send_json({"cmd": "step", "n": 3})
    pid = recv_until(ws, "tick")["positions"][0]["id"]
    ws.send_json({"cmd": "close", "position_id": pid})
    recv_until(ws, "tick")
    ws.send_json({"cmd": "step", "n": 3})
    t = recv_until(ws, "tick")
    assert t["new_trades"] or t["account"]["stats"]["trades"]


def journal_trades(client, **params):
    r = client.get("/journal/trades", params=params)
    assert r.status_code == 200, r.text
    return r.json()["trades"]


def test_closed_trades_are_recorded_while_playing(client):
    sid = new_session(client)
    assert journal_trades(client) == []
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        ws.receive_json()
        trade_once(client, ws)
        rows = journal_trades(client)
    assert len(rows) == 1
    t = rows[0]
    assert t["session_id"] == sid and t["side"] == "long" and t["instrument"] == "NQ"
    assert t["live"] is True and t["mae"] >= 0 and t["mfe"] >= 0
    assert t["day"] and t["point_value"] == 20.0
    assert client.get(f"/journal/trades/{t['id']}").json()["id"] == t["id"]


def test_mnq_trades_use_the_micro_point_value(client):
    sid = new_session(client, instrument="MNQ")
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        ws.receive_json()
        trade_once(client, ws)
    t = journal_trades(client)[0]
    assert t["instrument"] == "MNQ" and t["point_value"] == 2.0


def test_step_back_removes_the_undone_trade_but_keeps_noted_ones(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        ws.receive_json()
        trade_once(client, ws)
        assert len(journal_trades(client)) == 1
        ws.send_json({"cmd": "step_back", "n": 100})
        recv_until(ws, "reset")
        assert journal_trades(client) == []  # the trade no longer exists in the replay
        ws.send_json({"cmd": "step", "n": 10})  # orders need the replay to have started
        recv_until(ws, "tick")
        trade_once(client, ws)
        tid = journal_trades(client)[0]["id"]
        assert client.patch(f"/journal/trades/{tid}", json={"note": "keep"}).status_code == 200
        ws.send_json({"cmd": "step_back", "n": 100})
        recv_until(ws, "reset")
        assert journal_trades(client) == []  # hidden, not shown as a live trade
        assert (
            client.get(f"/journal/trades/{tid}").json()["note"] == "keep"
        )  # but the note survives


def test_seek_backwards_also_resyncs(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        ws.receive_json()
        start_ts = server.RUNTIMES[sid].ss.bars[0].ts // 10**9
        trade_once(client, ws)
        ws.send_json({"cmd": "seek", "ts": start_ts})
        recv_until(ws, "reset")
    assert journal_trades(client) == []


def test_deleting_a_session_keeps_its_trades(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        ws.receive_json()
        trade_once(client, ws)
    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert len(journal_trades(client)) == 1


def test_a_failing_journal_never_breaks_the_replay(client, monkeypatch):
    sid = new_session(client)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(journal.Journal, "sync", boom)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        assert ws.receive_json()["type"] in ("tick", "state")
        trade_once(client, ws)


# ---------------------------------------------------------------- REST
def seed(client):
    sid = new_session(client)
    with client.websocket_connect(f"/ws/{sid}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "step", "n": 10})
        ws.receive_json()
        trade_once(client, ws)
        trade_once(client, ws, side="short")
    return sid


def test_patch_notes_and_validation(client):
    seed(client)
    tid = journal_trades(client)[0]["id"]
    r = client.patch(
        f"/journal/trades/{tid}",
        json={"setup": "ORB", "emotion": "calm", "rating": 4, "mistakes": ["FOMO"], "note": "n"},
    )
    assert r.status_code == 200 and r.json()["setup"] == "ORB" and r.json()["mistakes"] == ["FOMO"]
    assert client.patch(f"/journal/trades/{tid}", json={"rating": 9}).status_code == 422
    assert client.patch(f"/journal/trades/{tid}", json={"setup": "x" * 41}).status_code == 422
    assert client.patch(f"/journal/trades/{tid}", json={"mistakes": "no"}).status_code == 422
    assert (
        client.patch(f"/journal/trades/{tid}", json={"pnl": 1e9}).status_code == 200
    )  # unknown keys ignored
    assert (
        client.get(f"/journal/trades/{tid}").json()["rating"] == 4
    )  # failed patches changed nothing
    assert client.patch("/journal/trades/99999", json={"note": "x"}).status_code == 404
    r = client.patch(f"/journal/trades/{tid}", json={"rating": None})
    assert r.json()["rating"] is None and r.json()["setup"] == "ORB"


def test_delete_trade(client):
    seed(client)
    tid = journal_trades(client)[0]["id"]
    assert client.delete(f"/journal/trades/{tid}").status_code == 200
    assert client.delete(f"/journal/trades/{tid}").status_code == 404
    assert client.get(f"/journal/trades/{tid}").status_code == 404
    assert len(journal_trades(client)) == 1


def test_list_filters_and_paging(client):
    sid = seed(client)
    assert len(journal_trades(client, session=sid)) == 2
    assert journal_trades(client, session="nope") == []
    assert [t["side"] for t in journal_trades(client, side="short")] == ["short"]
    assert len(journal_trades(client, limit=1)) == 1
    assert len(journal_trades(client, limit=1, offset=1)) == 1
    assert journal_trades(client, limit=1, offset=5) == []
    assert client.get("/journal/trades", params={"side": "sideways"}).status_code == 422
    assert client.get("/journal/trades", params={"limit": 0}).status_code == 422
    assert client.get("/journal/trades", params={"start": "not-a-date"}).status_code == 422


def test_analytics_and_calendar_endpoints(client):
    seed(client)
    a = client.get("/journal/analytics").json()
    assert a["summary"]["trades"] == 2 and len(a["equity"]) == 2
    assert set(a["breakdowns"]) >= {"hour", "weekday", "setup", "mistake", "side"}
    day = journal_trades(client)[0]["day"]
    y, m = int(day[:4]), int(day[5:7])
    cal = client.get("/journal/calendar", params={"year": y, "month": m}).json()
    assert cal["trades"] == 2 and cal["days"][0]["day"] == day
    assert client.get("/journal/calendar", params={"year": y, "month": 13}).status_code == 422
    assert client.get("/journal/calendar", params={"year": 1800, "month": 1}).status_code == 422
    assert (
        client.get("/journal/analytics", params={"start": day, "end": day}).json()["summary"][
            "trades"
        ]
        == 2
    )
    assert client.get(
        "/journal/analytics", params={"start": "2000-01-01", "end": "2000-01-02"}
    ).json()["summary"] == {"trades": 0}


def test_empty_journal_endpoints_work(client):
    assert client.get("/journal/analytics").json()["summary"] == {"trades": 0}
    assert client.get("/journal/tags").json() == {"setups": [], "emotions": [], "mistakes": []}
    assert client.get("/journal/calendar", params={"year": 2025, "month": 3}).json()["days"] == []


def test_tags_endpoint(client):
    seed(client)
    tid = journal_trades(client)[0]["id"]
    client.patch(f"/journal/trades/{tid}", json={"setup": "ORB", "mistakes": ["FOMO"]})
    assert client.get("/journal/tags").json()["setups"] == ["ORB"]


def test_replay_info_for_a_live_and_an_ended_session(client):
    sid = seed(client)
    tid = journal_trades(client)[0]["id"]
    r = client.get(f"/journal/trades/{tid}/replay").json()
    assert r["live"] is True and r["session_id"] == sid and r["request"]["source"] == "synthetic"
    assert r["request"]["bars"] == 1200 and r["opened"] <= r["closed"]
    client.delete(f"/sessions/{sid}")
    r = client.get(f"/journal/trades/{tid}/replay").json()
    assert r["live"] is False and r["request"]["tfs"] == [60, 300]  # enough to recreate the session
    assert client.get("/journal/trades/99999/replay").status_code == 404


def test_replay_request_recreates_a_session_at_the_same_trade(client):
    sid = seed(client)
    tid = journal_trades(client)[0]["id"]
    client.delete(f"/sessions/{sid}")
    info = client.get(f"/journal/trades/{tid}/replay").json()
    sid2 = client.post("/sessions", json=info["request"]).json()["id"]
    with client.websocket_connect(f"/ws/{sid2}") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "seek", "ts": info["opened"] - 5})
        recv_until(ws, "tick")  # a forward seek is an incremental update, not a reset
    ss2 = server.RUNTIMES[sid2].ss
    bar_ts = ss2.bars[ss2.cursor - 1].ts // 10**9
    assert (
        info["opened"] - 6 <= bar_ts <= info["opened"] - 4
    )  # the chart sits just before the entry


def test_text_in_notes_is_returned_as_data(client):
    seed(client)
    tid = journal_trades(client)[0]["id"]
    evil = "<img src=x onerror=alert(1)>"
    client.patch(f"/journal/trades/{tid}", json={"note": evil, "setup": "<b>x</b>"})
    t = client.get(f"/journal/trades/{tid}").json()
    assert t["note"] == evil and t["setup"] == "<b>x</b>"  # stored verbatim; the UI must escape it
