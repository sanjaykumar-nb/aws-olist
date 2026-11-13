"""Builds a tiny copy of the 9 Olist CSVs (with the real files' quirks) and a fresh DB per test."""

import pytest
from fastapi.testclient import TestClient

from app import config, loader, simulator

CSV = {
    "olist_customers_dataset.csv": (
        '"customer_id","customer_unique_id","customer_zip_code_prefix","customer_city","customer_state"\n'
        '"c1","u1","01037",sao paulo,SP\n'
        '"c2","u2","14409",franca,SP\n'
        '"c3","u3","09790",sao bernardo do campo,SP\n'
        '"c4","u1","01037",sao paulo,SP\n'
        '"c5","u5","22041",rio de janeiro,RJ\n'
    ),
    "olist_geolocation_dataset.csv": (
        '"geolocation_zip_code_prefix","geolocation_lat","geolocation_lng","geolocation_city","geolocation_state"\n'
        '"01037",-23.54562128115268,-46.63929204800168,sao paulo,SP\n'
        '"01037",-23.546,-46.639,sao paulo,SP\n'
        '"14409",-20.5,-47.4,franca,SP\n'
        '"22041",-22.97,-43.18,rio de janeiro,RJ\n'
    ),
    "olist_order_items_dataset.csv": (
        '"order_id","order_item_id","product_id","seller_id","shipping_limit_date","price","freight_value"\n'
        '"o1",1,"p1","s1",2017-09-19 09:45:35,58.90,13.29\n'
        '"o2",1,"p2","s2",2017-05-03 11:05:13,239.90,19.93\n'
        '"o2",2,"p3","s2",2017-05-03 11:05:13,10.00,5.00\n'
        '"o3",1,"p1","s1",2018-01-18 10:00:00,58.90,13.29\n'
    ),
    "olist_order_payments_dataset.csv": (
        '"order_id","payment_sequential","payment_type","payment_installments","payment_value"\n'
        'o1,1,credit_card,8,72.19\n'
        'o2,1,boleto,1,274.83\n'
        'o3,1,voucher,1,72.19\n'
    ),
    "olist_order_reviews_dataset.csv": (
        '"review_id","order_id","review_score","review_comment_title","review_comment_message",'
        '"review_creation_date","review_answer_timestamp"\n'
        '"r1","o1",4,,,2018-01-18 00:00:00,2018-01-18 21:46:59\n'
        '"r2","o2",1,"ruim","produto chegou\nquebrado",2018-03-10 00:00:00,2018-03-11 03:05:13\n'
        '"r3","o3",5,,"otimo",2018-02-17 00:00:00,2018-02-18 14:36:24\n'
    ),
    "olist_orders_dataset.csv": (
        '"order_id","customer_id","order_status","order_purchase_timestamp","order_approved_at",'
        '"order_delivered_carrier_date","order_delivered_customer_date","order_estimated_delivery_date"\n'
        'o1,"c1",delivered,2017-10-02 10:56:33,2017-10-02 11:07:15,2017-10-04 19:55:00,'
        '2017-10-10 21:25:13,2017-10-18 00:00:00\n'
        'o2,"c2",delivered,2018-07-24 20:41:37,2018-07-26 03:24:27,2018-07-26 14:31:00,'
        '2018-08-07 15:27:45,2018-08-13 00:00:00\n'
        'o3,"c3",shipped,2018-08-08 08:38:49,2018-08-08 08:55:23,2018-08-08 13:50:00,,2018-09-04 00:00:00\n'
        'o4,"c4",canceled,2018-02-13 21:18:39,,,,2018-03-06 00:00:00\n'
        'o5,"c5",invoiced,2018-08-01 10:00:00,2018-08-01 11:00:00,,,2018-08-20 00:00:00\n'
    ),
    "olist_products_dataset.csv": (
        '"product_id","product_category_name","product_name_lenght","product_description_lenght",'
        '"product_photos_qty","product_weight_g","product_length_cm","product_height_cm","product_width_cm"\n'
        '"p1",perfumaria,40,287,1,225,16,10,14\n'
        '"p2",artes,44,276,1,1000,30,18,20\n'
        '"p3",,,,,,,,\n'
    ),
    "olist_sellers_dataset.csv": (
        '"seller_id","seller_zip_code_prefix","seller_city","seller_state"\n'
        '"s1","13023",campinas,SP\n'
        's2,"01037",sao paulo,SP\n'
    ),
    "product_category_name_translation.csv": (
        "﻿product_category_name,product_category_name_english\n"
        "perfumaria,perfumery\n"
        "artes,art\n"
    ),
}


@pytest.fixture
def dataset_dir(tmp_path):
    d = tmp_path / "dataset"
    d.mkdir()
    for name, text in CSV.items():
        (d / name).write_text(text, encoding="utf-8", newline="\n")  # LF, like the real files
    return d


@pytest.fixture
def db(tmp_path, dataset_dir, monkeypatch):
    monkeypatch.setattr(config, "DATASET_DIR", dataset_dir)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    loader.load_all(log=lambda *_: None)
    return config.DB_PATH


@pytest.fixture
def fast_sim(monkeypatch):
    """Simulator with zero lifecycle delays and no randomness in cancel/address changes."""
    monkeypatch.setattr(simulator, "DELAYS", {k: (0, 0) for k in simulator.DELAYS})
    monkeypatch.setattr(simulator, "CANCEL_RATE", 0.0)
    return simulator.Simulator()


@pytest.fixture
def client(db):
    from app.main import app
    from app.simulator import simulator as global_sim

    with TestClient(app) as c:
        yield c
    global_sim.stop()
