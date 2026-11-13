"""Load the 9 Olist CSVs into SQLite.

Run directly:  python -m app.loader [--force]
The API also calls ensure_loaded() at startup, so a first run builds the DB automatically.
"""

import argparse
import csv
import sqlite3
import sys
import time

from . import config
from .db import AUDIT_COLUMN, DATASETS, connect, now_iso

CHUNK = 20_000

SYSTEM_DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    event_time  TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(event_type);

-- Orders created by the simulator that still have lifecycle steps left.
CREATE TABLE IF NOT EXISTS sim_inflight (
    order_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    next_status  TEXT NOT NULL,
    due_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _convert(value: str, sqltype: str):
    if value is None or value == "":
        return None
    if sqltype == "INTEGER":
        try:
            return int(float(value))
        except ValueError:
            return value
    if sqltype == "REAL":
        try:
            return float(value)
        except ValueError:
            return value
    return value


def is_loaded(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='loaded_at'").fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def load_dataset(conn: sqlite3.Connection, name: str, loaded_at: str) -> int:
    spec = DATASETS[name]
    path = config.DATASET_DIR / spec["file"]
    if not path.exists():
        raise FileNotFoundError(f"{path} not found (set OLIST_DATASET_DIR)")

    cols = spec["columns"]
    col_ddl = ", ".join(f'"{c}" {t}' for c, t in cols)
    conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.execute(f'CREATE TABLE "{name}" ({col_ddl}, "{AUDIT_COLUMN}" TEXT NOT NULL)')

    placeholders = ", ".join("?" for _ in range(len(cols) + 1))
    insert = f'INSERT INTO "{name}" VALUES ({placeholders})'

    total = 0
    batch = []
    # utf-8-sig strips the BOM in product_category_name_translation.csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        expected = [c for c, _ in cols]
        if header != expected:
            raise ValueError(f"{spec['file']}: header {header} != expected {expected}")
        for raw in reader:
            batch.append([_convert(v, t) for v, (_, t) in zip(raw, cols)] + [loaded_at])
            if len(batch) >= CHUNK:
                conn.executemany(insert, batch)
                total += len(batch)
                batch.clear()
        if batch:
            conn.executemany(insert, batch)
            total += len(batch)

    for col in spec["indexes"]:
        conn.execute(f'CREATE INDEX "ix_{name}_{col}" ON "{name}"("{col}")')
    conn.execute(f'CREATE INDEX "ix_{name}_{AUDIT_COLUMN}" ON "{name}"("{AUDIT_COLUMN}")')
    return total


def load_all(force: bool = False, log=print) -> None:
    conn = connect()
    try:
        conn.executescript(SYSTEM_DDL)
        if is_loaded(conn) and not force:
            log("Database already loaded (use --force to reload).")
            return
        loaded_at = now_iso()
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM sim_inflight")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='events'")
        for name in DATASETS:
            t0 = time.perf_counter()
            n = load_dataset(conn, name, loaded_at)
            conn.execute("INSERT INTO meta VALUES (?, ?)", (f"rows:{name}", str(n)))
            conn.commit()
            log(f"  {name:<22} {n:>9,} rows  ({time.perf_counter() - t0:.1f}s)")
        conn.execute("INSERT INTO meta VALUES ('loaded_at', ?)", (loaded_at,))
        conn.commit()
        log(f"Loaded into {config.DB_PATH}")
    finally:
        conn.close()


def ensure_loaded(log=print) -> None:
    conn = connect()
    try:
        conn.executescript(SYSTEM_DDL)
        loaded = is_loaded(conn)
    finally:
        conn.close()
    if not loaded:
        log(f"First run: loading CSVs from {config.DATASET_DIR} ...")
        load_all(log=log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load Olist CSVs into SQLite")
    parser.add_argument("--force", action="store_true", help="drop and reload everything")
    args = parser.parse_args()
    try:
        load_all(force=args.force)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
