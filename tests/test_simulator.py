import json

from app import simulator
from app.db import connect


def _run_lifecycle(sim, conn, rounds=10):
    for _ in range(rounds):
        sim._advance_due(conn)


def _events(conn):
    return [(r["event_type"], r["entity_id"], json.loads(r["payload"]))
            for r in conn.execute("SELECT * FROM events ORDER BY event_id")]


def test_new_order_writes_tables_and_events_together(db, fast_sim):
    conn = connect()
    fast_sim._prepare(conn)
    fast_sim._new_order(conn)

    order = conn.execute("SELECT * FROM orders WHERE rowid > 5").fetchone()
    assert order["order_status"] == "created"
    cust = conn.execute("SELECT * FROM customers WHERE customer_id=?", (order["customer_id"],)).fetchone()
    assert cust is not None
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order["order_id"],)).fetchall()
    pay = conn.execute("SELECT * FROM order_payments WHERE order_id=?", (order["order_id"],)).fetchone()
    assert 1 <= len(items) <= 4
    assert pay["payment_value"] == round(sum(i["price"] + i["freight_value"] for i in items), 2)

    types = [e[0] for e in _events(conn)]
    assert types == ["customer_created", "order_created", "payment_received"]
    created = _events(conn)[1][2]
    assert len(created["items"]) == len(items)


def test_full_lifecycle_in_order(db, fast_sim):
    conn = connect()
    fast_sim._prepare(conn)
    fast_sim._new_order(conn)
    _run_lifecycle(fast_sim, conn)

    order = conn.execute("SELECT * FROM orders WHERE rowid > 5").fetchone()
    assert order["order_status"] == "delivered"
    for col in ("order_approved_at", "order_delivered_carrier_date", "order_delivered_customer_date"):
        assert order[col] is not None
    assert conn.execute("SELECT count(*) FROM order_reviews WHERE order_id=?",
                        (order["order_id"],)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM sim_inflight").fetchone()[0] == 0

    order_events = [t for t, eid, p in _events(conn)
                    if eid == order["order_id"] or p.get("order_id") == order["order_id"]]
    assert order_events == ["order_created", "payment_received", "order_approved",
                            "order_shipped", "order_delivered", "review_submitted"]


def test_cancel_path(db, fast_sim, monkeypatch):
    monkeypatch.setattr(simulator, "CANCEL_RATE", 1.0)
    conn = connect()
    fast_sim._prepare(conn)
    fast_sim._new_order(conn)
    _run_lifecycle(fast_sim, conn)
    order = conn.execute("SELECT * FROM orders WHERE rowid > 5").fetchone()
    assert order["order_status"] == "canceled"
    assert [e[0] for e in _events(conn)][-1] == "order_canceled"
    assert conn.execute("SELECT count(*) FROM sim_inflight").fetchone()[0] == 0


def test_status_changes_bump_updated_at(db, fast_sim):
    conn = connect()
    fast_sim._prepare(conn)
    fast_sim._new_order(conn)
    before = conn.execute("SELECT updated_at FROM orders WHERE rowid > 5").fetchone()[0]
    fast_sim._advance_due(conn)
    after = conn.execute("SELECT updated_at FROM orders WHERE rowid > 5").fetchone()[0]
    assert after > before


def test_address_change_updates_row_and_emits_before_after(db, fast_sim):
    conn = connect()
    fast_sim._prepare(conn)
    fast_sim._address_change(conn)
    (etype, cid, payload), = _events(conn)
    assert etype == "customer_address_changed"
    row = conn.execute("SELECT * FROM customers WHERE customer_id=?", (cid,)).fetchone()
    assert row["customer_city"] == payload["after"]["customer_city"]
    assert row["updated_at"] == payload["updated_at"]
    assert set(payload["before"]) == set(payload["after"])


def test_event_ids_strictly_increase(db, fast_sim):
    conn = connect()
    fast_sim._prepare(conn)
    for _ in range(5):
        fast_sim._new_order(conn)
    _run_lifecycle(fast_sim, conn)
    ids = [r[0] for r in conn.execute("SELECT event_id FROM events ORDER BY rowid")]
    assert ids == sorted(ids) and len(ids) == len(set(ids)) == 5 * 6 + 5  # +customer_created


def test_rate_is_clamped(fast_sim):
    fast_sim.set_rate(1000)
    assert fast_sim.rate == 50.0
    fast_sim.set_rate(0)
    assert fast_sim.rate == 0.05


def test_start_on_empty_db_reports_error(tmp_path, monkeypatch):
    from app import config, loader
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "empty.db")
    conn = connect()
    conn.executescript(loader.SYSTEM_DDL)
    for t in ("customers", "order_items", "order_reviews", "geolocation"):
        conn.execute(f"CREATE TABLE {t} (x)")
    conn.commit()
    sim = simulator.Simulator()
    sim.start()
    sim._thread.join(2)
    assert not sim.running
    assert "empty" in sim.last_error
