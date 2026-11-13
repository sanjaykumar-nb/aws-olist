"""API key, autostart and event retention (the settings used on EC2)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import config, simulator
from app.db import ISO_FMT, connect

API = "/api/v1"
KEY = "s3cret-key"


@pytest.fixture
def keyed_client(db, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", KEY)
    from app.main import app
    with TestClient(app) as c:
        yield c
    simulator.simulator.stop()


def test_health_and_ui_stay_open(keyed_client):
    r = keyed_client.get(f"{API}/health")
    assert r.status_code == 200 and r.json()["auth_required"] is True
    assert keyed_client.get("/").status_code == 200
    assert keyed_client.get("/static/app.js").status_code == 200


@pytest.mark.parametrize("method,path", [
    ("get", "/datasets"), ("get", "/data/orders"), ("get", "/export/orders.csv"),
    ("get", "/events"), ("get", "/stream/status"), ("post", "/stream/start"),
    ("post", "/stream/stop"), ("get", "/stream/events"), ("get", "/auth"),
])
def test_everything_else_needs_the_key(keyed_client, method, path):
    r = getattr(keyed_client, method)(f"{API}{path}")
    assert r.status_code == 401
    r = getattr(keyed_client, method)(f"{API}{path}", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_key_via_header_or_query(keyed_client):
    assert keyed_client.get(f"{API}/data/orders", headers={"X-API-Key": KEY}).status_code == 200
    r = keyed_client.get(f"{API}/export/orders.csv", params={"api_key": KEY})
    assert r.status_code == 200
    # api_key is not mistaken for a column filter
    assert r.text.count("\n") == 6  # header + 5 rows
    assert keyed_client.get(f"{API}/auth", headers={"X-API-Key": KEY}).json() == {"ok": True}


def test_no_key_configured_means_open(client):
    assert client.get(f"{API}/health").json()["auth_required"] is False
    assert client.get(f"{API}/datasets").status_code == 200


def test_autostart(db, monkeypatch):
    monkeypatch.setattr(config, "AUTOSTART", True)
    from app.main import app
    with TestClient(app) as c:
        assert c.get(f"{API}/stream/status").json()["running"] is True
    assert not simulator.simulator.running  # stopped on shutdown


def _event(conn, age_hours):
    t = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime(ISO_FMT)
    conn.execute("INSERT INTO events(event_type, entity, entity_id, event_time, payload) "
                 "VALUES ('x','order','o1',?, ?)", (t, json.dumps({})))
    conn.commit()


def test_retention_deletes_only_old_events(db, monkeypatch):
    monkeypatch.setattr(config, "EVENT_RETENTION_HOURS", 24)
    conn = connect()
    _event(conn, 48)
    _event(conn, 30)
    _event(conn, 1)
    assert simulator.prune_events(conn) == 2
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_retention_zero_keeps_everything(db, monkeypatch):
    monkeypatch.setattr(config, "EVENT_RETENTION_HOURS", 0)
    conn = connect()
    _event(conn, 10_000)
    assert simulator.prune_events(conn) == 0
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
