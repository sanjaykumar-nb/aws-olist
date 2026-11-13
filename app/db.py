"""SQLite connection helper and the dataset catalog (one entry per source CSV)."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

# name -> (csv file, [(column, sqlite type)], [index columns])
# Column names are kept exactly as in the source files (including the original
# "lenght" misspellings) so this behaves like a real, unpolished source system.
DATASETS: dict[str, dict] = {
    "customers": {
        "file": "olist_customers_dataset.csv",
        "columns": [
            ("customer_id", "TEXT"),
            ("customer_unique_id", "TEXT"),
            ("customer_zip_code_prefix", "TEXT"),
            ("customer_city", "TEXT"),
            ("customer_state", "TEXT"),
        ],
        "indexes": ["customer_id"],
    },
    "geolocation": {
        "file": "olist_geolocation_dataset.csv",
        "columns": [
            ("geolocation_zip_code_prefix", "TEXT"),
            ("geolocation_lat", "REAL"),
            ("geolocation_lng", "REAL"),
            ("geolocation_city", "TEXT"),
            ("geolocation_state", "TEXT"),
        ],
        "indexes": ["geolocation_zip_code_prefix"],
    },
    "order_items": {
        "file": "olist_order_items_dataset.csv",
        "columns": [
            ("order_id", "TEXT"),
            ("order_item_id", "INTEGER"),
            ("product_id", "TEXT"),
            ("seller_id", "TEXT"),
            ("shipping_limit_date", "TEXT"),
            ("price", "REAL"),
            ("freight_value", "REAL"),
        ],
        "indexes": ["order_id"],
    },
    "order_payments": {
        "file": "olist_order_payments_dataset.csv",
        "columns": [
            ("order_id", "TEXT"),
            ("payment_sequential", "INTEGER"),
            ("payment_type", "TEXT"),
            ("payment_installments", "INTEGER"),
            ("payment_value", "REAL"),
        ],
        "indexes": ["order_id"],
    },
    "order_reviews": {
        "file": "olist_order_reviews_dataset.csv",
        "columns": [
            ("review_id", "TEXT"),
            ("order_id", "TEXT"),
            ("review_score", "INTEGER"),
            ("review_comment_title", "TEXT"),
            ("review_comment_message", "TEXT"),
            ("review_creation_date", "TEXT"),
            ("review_answer_timestamp", "TEXT"),
        ],
        "indexes": ["order_id", "review_id"],
    },
    "orders": {
        "file": "olist_orders_dataset.csv",
        "columns": [
            ("order_id", "TEXT"),
            ("customer_id", "TEXT"),
            ("order_status", "TEXT"),
            ("order_purchase_timestamp", "TEXT"),
            ("order_approved_at", "TEXT"),
            ("order_delivered_carrier_date", "TEXT"),
            ("order_delivered_customer_date", "TEXT"),
            ("order_estimated_delivery_date", "TEXT"),
        ],
        "indexes": ["order_id", "customer_id", "order_status"],
    },
    "products": {
        "file": "olist_products_dataset.csv",
        "columns": [
            ("product_id", "TEXT"),
            ("product_category_name", "TEXT"),
            ("product_name_lenght", "INTEGER"),
            ("product_description_lenght", "INTEGER"),
            ("product_photos_qty", "INTEGER"),
            ("product_weight_g", "INTEGER"),
            ("product_length_cm", "INTEGER"),
            ("product_height_cm", "INTEGER"),
            ("product_width_cm", "INTEGER"),
        ],
        "indexes": ["product_id"],
    },
    "sellers": {
        "file": "olist_sellers_dataset.csv",
        "columns": [
            ("seller_id", "TEXT"),
            ("seller_zip_code_prefix", "TEXT"),
            ("seller_city", "TEXT"),
            ("seller_state", "TEXT"),
        ],
        "indexes": ["seller_id"],
    },
    "category_translation": {
        "file": "product_category_name_translation.csv",
        "columns": [
            ("product_category_name", "TEXT"),
            ("product_category_name_english", "TEXT"),
        ],
        "indexes": ["product_category_name"],
    },
}

# Every source table also gets this audit column, set on insert and on every update.
# It is what makes incremental pulls (?updated_since=...) possible.
AUDIT_COLUMN = "updated_at"


ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Incremental-pull safety margin. A row is stamped just before its transaction commits,
# so a reader can briefly see a server time later than a row it cannot see yet. Watermarks
# handed to clients are pulled back by this much; overlapping rows are re-sent, never lost.
WATERMARK_LAG = timedelta(seconds=5)


def now_iso() -> str:
    """UTC timestamp with microseconds, sortable as text."""
    return datetime.now(timezone.utc).strftime(ISO_FMT)


def watermark_iso() -> str:
    """Safe value for a client's next ?updated_since=."""
    return (datetime.now(timezone.utc) - WATERMARK_LAG).strftime(ISO_FMT)


def normalize_ts(value: str) -> str:
    """Parse any ISO-8601 date/datetime and return it in the stored format (UTC, microseconds),
    so text comparison against updated_at is exact. Naive values are treated as UTC.
    Raises ValueError on bad input."""
    dt = datetime.fromisoformat(value.strip().replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FMT)


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def column_names(dataset: str) -> list[str]:
    return [c for c, _ in DATASETS[dataset]["columns"]] + [AUDIT_COLUMN]
