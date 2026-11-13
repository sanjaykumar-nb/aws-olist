import csv
import io
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.db import connect

API = "/api/v1"


def test_health(client):
    r = client.get(f"{API}/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_datasets(client):
    ds = {d["name"]: d for d in client.get(f"{API}/datasets").json()["datasets"]}
    assert len(ds) == 9
    assert ds["orders"]["row_count"] == 5
    assert ds["orders"]["loaded_row_count"] == 5 and ds["orders"]["simulated_row_count"] == 0
    assert ds["orders"]["columns"][-1]["name"] == "updated_at"


def test_unknown_dataset_404(client):
    assert client.get(f"{API}/data/nope").status_code == 404
    assert client.get(f"{API}/export/nope.csv").status_code == 404


def test_cursor_pagination_walks_every_row_once(client):
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = client.get(f"{API}/data/orders", params=params).json()
        seen += [r["order_id"] for r in body["data"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ["o1", "o2", "o3", "o4", "o5"]


def test_limit_bounds(client):
    assert client.get(f"{API}/data/orders", params={"limit": 0}).status_code == 422
    assert client.get(f"{API}/data/orders", params={"limit": 10_001}).status_code == 422


def test_column_filter(client):
    body = client.get(f"{API}/data/orders", params={"order_status": "delivered"}).json()
    assert {r["order_id"] for r in body["data"]} == {"o1", "o2"}
    # unknown params are ignored rather than breaking the query
    assert client.get(f"{API}/data/orders", params={"bogus": "x"}).json()["count"] == 5


def _touch(order_id, stamp):
    conn = connect()
    conn.execute("UPDATE orders SET updated_at=? WHERE order_id=?", (stamp, order_id))
    conn.commit()


def test_updated_since_accepts_any_iso_form(client):
    _touch("o3", "2030-01-01T10:00:00.500000Z")
    for since in ("2030-01-01T10:00:00Z", "2030-01-01T10:00:00", "2030-01-01 10:00:00",
                  "2030-01-01T15:30:00+05:30", "2030-01-01"):
        body = client.get(f"{API}/data/orders", params={"updated_since": since}).json()
        assert [r["order_id"] for r in body["data"]] == ["o3"], since
    body = client.get(f"{API}/data/orders", params={"updated_since": "2030-01-01T10:00:01Z"}).json()
    assert body["count"] == 0


def test_updated_since_rejects_garbage(client):
    r = client.get(f"{API}/data/orders", params={"updated_since": "yesterday"})
    assert r.status_code == 422
    assert client.get(f"{API}/export/orders.csv", params={"updated_since": "x"}).status_code == 422


def test_watermark_lags_server_time(client):
    body = client.get(f"{API}/data/orders").json()
    now = client.get(f"{API}/health").json()["server_time"]
    assert body["watermark"] < now


def test_csv_export_full_and_filtered(client):
    r = client.get(f"{API}/export/order_reviews.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "x-watermark" in r.headers
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 3
    assert rows[1]["review_comment_message"] == "produto chegou\nquebrado"
    assert list(rows[0])[-1] == "updated_at"

    r = client.get(f"{API}/export/orders.csv", params={"order_status": "canceled"})
    assert [row["order_id"] for row in csv.DictReader(io.StringIO(r.text))] == ["o4"]


def test_csv_export_incremental(client):
    _touch("o5", "2030-01-01T00:00:00.000000Z")
    r = client.get(f"{API}/export/orders.csv", params={"updated_since": "2029-12-31"})
    assert 'orders_incremental.csv' in r.headers["content-disposition"]
    assert [row["order_id"] for row in csv.DictReader(io.StringIO(r.text))] == ["o5"]


def test_stream_start_status_rate_stop(client):
    r = client.post(f"{API}/stream/start", json={"orders_per_sec": 20})
    assert r.json()["running"] is True and r.json()["orders_per_sec"] == 20
    # starting twice is harmless
    assert client.post(f"{API}/stream/start").json()["running"] is True

    deadline = time.time() + 5
    while time.time() < deadline and client.get(f"{API}/stream/status").json()["total_events"] < 10:
        time.sleep(0.1)
    status = client.get(f"{API}/stream/status").json()
    assert status["total_events"] >= 10 and status["last_error"] is None

    assert client.post(f"{API}/stream/rate", json={"orders_per_sec": 3}).json()["orders_per_sec"] == 3
    assert client.post(f"{API}/stream/rate", json={}).status_code == 422
    assert client.post(f"{API}/stream/start", json={"orders_per_sec": 999}).status_code == 422

    assert client.post(f"{API}/stream/stop").json()["running"] is False
    n = client.get(f"{API}/stream/status").json()["total_events"]
    time.sleep(0.6)
    assert client.get(f"{API}/stream/status").json()["total_events"] == n  # really stopped

    ds = {d["name"]: d for d in client.get(f"{API}/datasets").json()["datasets"]}
    assert ds["orders"]["simulated_row_count"] > 0


def test_events_pull_with_offset_and_types(client):
    client.post(f"{API}/stream/start", json={"orders_per_sec": 20})
    deadline = time.time() + 5
    while time.time() < deadline and client.get(f"{API}/events").json()["count"] < 9:
        time.sleep(0.1)
    client.post(f"{API}/stream/stop")

    first = client.get(f"{API}/events", params={"limit": 3}).json()
    assert [e["event_id"] for e in first["events"]] == [1, 2, 3]
    assert first["next_after_id"] == 3
    nxt = client.get(f"{API}/events", params={"after_id": 3, "limit": 3}).json()
    assert nxt["events"][0]["event_id"] == 4
    assert isinstance(nxt["events"][0]["payload"], dict)

    only = client.get(f"{API}/events", params={"types": "order_created,payment_received"}).json()
    assert only["count"] > 0
    assert {e["event_type"] for e in only["events"]} <= {"order_created", "payment_received"}

    empty = client.get(f"{API}/events", params={"after_id": 10**9}).json()
    assert empty == {"count": 0, "next_after_id": 10**9, "events": []}


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


# ---------------------------------------------------------------- SSE over a real server
@pytest.fixture
def live_server(db):
    from app.main import app
    from app.simulator import simulator

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning",
                                           timeout_graceful_shutdown=1))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    simulator.stop()
    server.should_exit = True
    t.join(5)


def _read_sse(base, n, params=None, headers=None):
    out = []
    with httpx.stream("GET", f"{base}{API}/stream/events", params=params, headers=headers,
                      timeout=10) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
                if len(out) == n:
                    break
    return out


def test_sse_pushes_live_events_and_resumes(live_server):
    httpx.post(f"{live_server}{API}/stream/start", json={"orders_per_sec": 20})
    events = _read_sse(live_server, 5)
    ids = [e["event_id"] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == 5

    # resume from an offset: the next event is exactly after_id + 1
    resumed = _read_sse(live_server, 2, params={"after_id": ids[0]})
    assert resumed[0]["event_id"] == ids[0] + 1
    # Last-Event-ID header (what EventSource sends on reconnect) wins
    resumed = _read_sse(live_server, 1, headers={"Last-Event-ID": str(ids[1])})
    assert resumed[0]["event_id"] == ids[1] + 1

    typed = _read_sse(live_server, 3, params={"types": "order_created"})
    assert {e["event_type"] for e in typed} == {"order_created"}
