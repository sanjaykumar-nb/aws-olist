import pytest

from app import config, loader
from app.db import connect


def test_loads_every_dataset_with_row_counts(db):
    expected = {
        "customers": 5, "geolocation": 4, "order_items": 4, "order_payments": 3,
        "order_reviews": 3, "orders": 5, "products": 3, "sellers": 2, "category_translation": 2,
    }
    conn = connect()
    for table, n in expected.items():
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == n
        assert conn.execute(f"SELECT value FROM meta WHERE key='rows:{table}'").fetchone()[0] == str(n)


def test_source_quirks_are_preserved(db):
    conn = connect()
    # leading zeros kept as text
    assert conn.execute("SELECT customer_zip_code_prefix FROM customers WHERE customer_id='c1'"
                        ).fetchone()[0] == "01037"
    # multi-line review comment survives
    msg = conn.execute("SELECT review_comment_message FROM order_reviews WHERE review_id='r2'").fetchone()[0]
    assert msg == "produto chegou\nquebrado"
    # empty -> NULL, numbers typed
    row = conn.execute("SELECT * FROM products WHERE product_id='p3'").fetchone()
    assert row["product_category_name"] is None and row["product_weight_g"] is None
    price = conn.execute("SELECT price FROM order_items WHERE order_id='o1'").fetchone()[0]
    assert isinstance(price, float) and price == pytest.approx(58.90)
    # BOM stripped from the translation file header (column name is clean)
    assert conn.execute("SELECT product_category_name_english FROM category_translation "
                        "WHERE product_category_name='artes'").fetchone()[0] == "art"
    # audit column is set
    assert conn.execute("SELECT count(*) FROM orders WHERE updated_at IS NULL").fetchone()[0] == 0


def test_second_load_is_a_noop_and_force_reloads(db):
    conn = connect()
    conn.execute("DELETE FROM sellers")
    conn.commit()
    loader.load_all(log=lambda *_: None)
    assert conn.execute("SELECT count(*) FROM sellers").fetchone()[0] == 0
    loader.load_all(force=True, log=lambda *_: None)
    assert conn.execute("SELECT count(*) FROM sellers").fetchone()[0] == 2


def test_header_mismatch_is_rejected(db):
    (config.DATASET_DIR / "olist_sellers_dataset.csv").write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="header"):
        loader.load_all(force=True, log=lambda *_: None)


def test_missing_file_is_reported(db):
    (config.DATASET_DIR / "olist_sellers_dataset.csv").unlink()
    with pytest.raises(FileNotFoundError):
        loader.load_all(force=True, log=lambda *_: None)
