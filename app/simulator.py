"""Streaming simulator: generates live e-commerce activity on top of the historical data.

While running it creates new orders (with a new customer row, items and payments) and
moves earlier simulated orders through their lifecycle:

    created -> approved -> shipped -> delivered -> review submitted
            \\-> canceled (about 3%)

Every change is written to the source tables (with a fresh updated_at) AND appended to
the `events` table in the same transaction (outbox pattern), so consumers can read either
the changed rows (batch, ?updated_since=) or the event log (streaming).

Values are sampled from the historical data (existing customers, product/seller/price
rows, review texts), so the new data looks like the old data. Timestamps are real
wall-clock time.
"""

import json
import logging
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import config
from .db import connect, now_iso

# Lifecycle step delays in real seconds: (min, max)
DELAYS = {
    "approved": (3, 10),
    "canceled": (5, 15),
    "shipped": (8, 25),
    "delivered": (15, 45),
    "review": (5, 20),
}
CANCEL_RATE = 0.03
REPEAT_BUYER_RATE = 0.85  # share of new orders placed by an existing customer_unique_id
ADDRESS_CHANGE_RATE = 0.05  # per new order, chance an existing customer changes address
PAYMENT_TYPES = [("credit_card", 0.74), ("boleto", 0.19), ("voucher", 0.055), ("debit_card", 0.015)]
TICK = 0.25

log = logging.getLogger("olist.simulator")


def _ts(dt: datetime | None = None) -> str:
    """Timestamp in the same format as the source CSVs."""
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


class Simulator:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._counts_lock = threading.Lock()
        self.rate = config.DEFAULT_ORDERS_PER_SEC
        self.started_at: str | None = None
        self.counts: dict[str, int] = {}
        self.last_error: str | None = None
        self._max_rowid: dict[str, int] = {}

    # ---------- control ----------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, rate: float | None = None) -> None:
        with self._lock:
            if rate is not None:
                self.set_rate(rate)
            if self.running:
                return
            self._stop.clear()
            self.started_at = now_iso()
            self.counts = {}
            self.last_error = None
            self._thread = threading.Thread(target=self._run, name="simulator", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=5)
            self._thread = None

    def set_rate(self, rate: float) -> None:
        self.rate = max(0.05, min(float(rate), config.MAX_ORDERS_PER_SEC))

    def status(self) -> dict:
        return {
            "running": self.running,
            "orders_per_sec": self.rate,
            "started_at": self.started_at if self.running else None,
            "events_this_run": self._counts_snapshot(),
            "last_error": self.last_error,
        }

    # ---------- loop ----------
    def _run(self) -> None:
        conn = connect()
        try:
            try:
                self._prepare(conn)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                return
            pending = 0.0
            last = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                pending += self.rate * (now - last) * random.uniform(0.6, 1.4)
                last = now
                try:
                    while pending >= 1:
                        pending -= 1
                        self._new_order(conn)
                        if random.random() < ADDRESS_CHANGE_RATE:
                            self._address_change(conn)
                    self._advance_due(conn)
                except Exception as e:  # keep streaming; surface the error in /stream/status
                    conn.rollback()
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.exception("simulator step failed")
                self._stop.wait(TICK)
        finally:
            conn.close()

    # ---------- helpers ----------
    def _prepare(self, conn) -> None:
        """Cache the historical rowid range used for sampling."""
        for t in ("customers", "order_items", "order_reviews", "geolocation"):
            n = conn.execute(f"SELECT max(rowid) FROM {t}").fetchone()[0] or 0
            if n == 0:
                raise RuntimeError(f"table '{t}' is empty; load the dataset first")
            self._max_rowid[t] = n

    def _random_row(self, conn, table: str) -> sqlite3.Row:
        while True:
            rid = random.randint(1, self._max_rowid[table])
            row = conn.execute(f"SELECT * FROM {table} WHERE rowid=?", (rid,)).fetchone()
            if row is not None:
                return row

    def _emit(self, conn, event_type: str, entity: str, entity_id: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO events(event_type, entity, entity_id, event_time, payload) VALUES (?,?,?,?,?)",
            (event_type, entity, entity_id, now_iso(), json.dumps(payload)),
        )
        with self._counts_lock:
            self.counts[event_type] = self.counts.get(event_type, 0) + 1

    def _counts_snapshot(self) -> dict[str, int]:
        with self._counts_lock:
            return dict(self.counts)

    @staticmethod
    def _insert(conn, table: str, row: dict) -> None:
        cols = ", ".join(f'"{c}"' for c in row)
        conn.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({", ".join("?" * len(row))})',
                     list(row.values()))

    @staticmethod
    def _schedule(conn, order_id: str, customer_id: str, status: str) -> None:
        lo, hi = DELAYS[status]
        conn.execute(
            "INSERT OR REPLACE INTO sim_inflight VALUES (?,?,?,?)",
            (order_id, customer_id, status, time.time() + random.uniform(lo, hi)),
        )

    # ---------- generators ----------
    def _new_order(self, conn) -> None:
        stamp = now_iso()
        now = datetime.now()

        # Olist issues a new customer_id per order; customer_unique_id is the real person.
        base = self._random_row(conn, "customers")
        if random.random() < REPEAT_BUYER_RATE:
            unique_id = base["customer_unique_id"]
            zip_, city, state = (base["customer_zip_code_prefix"], base["customer_city"],
                                 base["customer_state"])
        else:
            unique_id = uuid.uuid4().hex
            geo = self._random_row(conn, "geolocation")
            zip_, city, state = (geo["geolocation_zip_code_prefix"], geo["geolocation_city"],
                                 geo["geolocation_state"])
        customer = {
            "customer_id": uuid.uuid4().hex,
            "customer_unique_id": unique_id,
            "customer_zip_code_prefix": zip_,
            "customer_city": city,
            "customer_state": state,
            "updated_at": stamp,
        }

        order_id = uuid.uuid4().hex
        order = {
            "order_id": order_id,
            "customer_id": customer["customer_id"],
            "order_status": "created",
            "order_purchase_timestamp": _ts(now),
            "order_approved_at": None,
            "order_delivered_carrier_date": None,
            "order_delivered_customer_date": None,
            "order_estimated_delivery_date": _ts(
                (now + timedelta(days=random.randint(7, 35))).replace(hour=0, minute=0, second=0)
            ),
            "updated_at": stamp,
        }

        n_items = random.choices([1, 2, 3, 4], weights=[85, 10, 4, 1])[0]
        items = []
        for i in range(1, n_items + 1):
            src = self._random_row(conn, "order_items")  # realistic product/seller/price combo
            items.append({
                "order_id": order_id,
                "order_item_id": i,
                "product_id": src["product_id"],
                "seller_id": src["seller_id"],
                "shipping_limit_date": _ts(now + timedelta(days=random.randint(2, 7))),
                "price": src["price"],
                "freight_value": src["freight_value"],
                "updated_at": stamp,
            })
        total = round(sum(i["price"] + i["freight_value"] for i in items), 2)

        ptype = random.choices([p for p, _ in PAYMENT_TYPES], [w for _, w in PAYMENT_TYPES])[0]
        payment = {
            "order_id": order_id,
            "payment_sequential": 1,
            "payment_type": ptype,
            "payment_installments": random.randint(1, 10) if ptype == "credit_card" else 1,
            "payment_value": total,
            "updated_at": stamp,
        }

        self._insert(conn, "customers", customer)
        self._insert(conn, "orders", order)
        for it in items:
            self._insert(conn, "order_items", it)
        self._insert(conn, "order_payments", payment)

        self._emit(conn, "customer_created", "customer", customer["customer_id"], customer)
        self._emit(conn, "order_created", "order", order_id, {**order, "items": items})
        self._emit(conn, "payment_received", "payment", order_id, payment)

        nxt = "canceled" if random.random() < CANCEL_RATE else "approved"
        self._schedule(conn, order_id, customer["customer_id"], nxt)
        conn.commit()

    def _address_change(self, conn) -> None:
        cust = self._random_row(conn, "customers")
        geo = self._random_row(conn, "geolocation")
        stamp = now_iso()
        before = {k: cust[k] for k in ("customer_zip_code_prefix", "customer_city", "customer_state")}
        after = {
            "customer_zip_code_prefix": geo["geolocation_zip_code_prefix"],
            "customer_city": geo["geolocation_city"],
            "customer_state": geo["geolocation_state"],
        }
        conn.execute(
            "UPDATE customers SET customer_zip_code_prefix=?, customer_city=?, customer_state=?, "
            "updated_at=? WHERE rowid=(SELECT rowid FROM customers WHERE customer_id=? LIMIT 1)",
            (*after.values(), stamp, cust["customer_id"]),
        )
        self._emit(conn, "customer_address_changed", "customer", cust["customer_id"],
                   {"customer_id": cust["customer_id"], "before": before, "after": after,
                    "updated_at": stamp})
        conn.commit()

    def _advance_due(self, conn) -> None:
        due = conn.execute(
            "SELECT order_id, customer_id, next_status FROM sim_inflight "
            "WHERE due_at <= ? ORDER BY due_at LIMIT 500",
            (time.time(),),
        ).fetchall()
        for order_id, customer_id, status in due:
            stamp = now_iso()
            ts = _ts()
            if status == "review":
                src = self._random_row(conn, "order_reviews")
                review = {
                    "review_id": uuid.uuid4().hex,
                    "order_id": order_id,
                    "review_score": src["review_score"],
                    "review_comment_title": src["review_comment_title"],
                    "review_comment_message": src["review_comment_message"],
                    "review_creation_date": _ts(datetime.now().replace(hour=0, minute=0, second=0)),
                    "review_answer_timestamp": ts,
                    "updated_at": stamp,
                }
                self._insert(conn, "order_reviews", review)
                self._emit(conn, "review_submitted", "review", review["review_id"], review)
                conn.execute("DELETE FROM sim_inflight WHERE order_id=?", (order_id,))
                continue

            col = {
                "approved": "order_approved_at",
                "shipped": "order_delivered_carrier_date",
                "delivered": "order_delivered_customer_date",
                "canceled": None,
            }[status]
            if col:
                conn.execute(f"UPDATE orders SET order_status=?, {col}=?, updated_at=? "
                             "WHERE order_id=?", (status, ts, stamp, order_id))
            else:
                conn.execute("UPDATE orders SET order_status=?, updated_at=? WHERE order_id=?",
                             (status, stamp, order_id))
            payload = {"order_id": order_id, "customer_id": customer_id, "order_status": status,
                       "updated_at": stamp}
            if col:
                payload[col] = ts
            self._emit(conn, f"order_{status}", "order", order_id, payload)

            following = {"approved": "shipped", "shipped": "delivered", "delivered": "review"}
            if status in following:
                self._schedule(conn, order_id, customer_id, following[status])
            else:
                conn.execute("DELETE FROM sim_inflight WHERE order_id=?", (order_id,))
        if due:
            conn.commit()


simulator = Simulator()
