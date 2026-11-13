# Olist Source API

A simple **source system** for the Olist e-commerce dataset (9 CSVs). It serves the data
over a REST API, exports it as CSV, and simulates a live stream of new activity, so an
external pipeline (e.g. on AWS) can pull batch data and consume streaming data from it.

It does no processing or transformation: the data is served as-is, the way a real
operational system would expose it.

**Dataset:** the CSVs are not in this repo. Download the
[Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
from Kaggle and put the 9 CSV files in `./dataset`, or point `OLIST_DATASET_DIR` (or
`OLIST_DATASET_HOST_DIR` for Docker) at the folder that holds them.

## Run with Docker

```bash
cp .env.example .env        # set OLIST_DATASET_HOST_DIR to the folder with the 9 CSVs
docker compose up -d --build
```

- UI: http://localhost:8000/
- Swagger docs: http://localhost:8000/docs

The CSV folder is mounted read-only at `/dataset`. The SQLite DB lives in the named volume
`olist-data`, so loaded and simulated data survive restarts and rebuilds. On first start the
container loads the CSVs (about 20 s; the healthcheck allows 90 s). To start from a clean
dataset, run `docker compose down -v` and then `docker compose up -d`.

The container runs as a non-root user with **one** uvicorn worker. The simulator lives
inside the process and SQLite has a single writer, so do not scale this service to more
than one replica.

> If `pip install` fails during the build with `Network is unreachable`, Docker's bridge
> network has no internet access on that machine. Build with host networking instead:
> `docker build --network host -t olist-source:latest .` then `docker compose up -d`.

## Run without Docker

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

On first start the 9 CSVs are loaded into `data/olist.db`.
Reload them at any time with `python -m app.loader --force` (stop the server first). This
wipes all simulated data.

| Env var | Default |
|---|---|
| `OLIST_DATASET_DIR` | `./dataset` (`/dataset` in Docker) |
| `OLIST_DB_PATH` | `data/olist.db` (`/data/olist.db` in Docker) |
| `OLIST_ORDERS_PER_SEC` | `1` |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests build a small copy of the 9 CSVs, including the real files' quirks (BOM,
multi-line review text, leading-zero zip codes, empty fields). They cover the loader, every
endpoint, pagination, incremental filters, CSV export, the full order lifecycle,
start/stop/rate, the event log, and the SSE feed over a real server.

## Datasets

`customers`, `geolocation`, `order_items`, `order_payments`, `order_reviews`, `orders`,
`products`, `sellers`, `category_translation`.

Columns are exactly as in the CSVs (including the source's `product_name_lenght` typo),
plus one audit column, **`updated_at`** (UTC ISO), set on load and on every insert or update.
Historical rows all share the load time.

## Endpoints (`/api/v1`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and server time |
| GET | `/datasets` | Datasets, row counts (total, loaded, simulated), columns, last update |
| GET | `/data/{dataset}` | JSON rows. `limit` (≤10 000), `cursor`, `updated_since`, and any `column=value` filter |
| GET | `/export/{dataset}.csv` | Streamed CSV. Same filters, including `updated_since` for incremental |
| POST | `/stream/start` | Start the simulator. Body `{"orders_per_sec": 2}` (optional) |
| POST | `/stream/stop` | Stop it |
| POST | `/stream/rate` | Change the rate while running |
| GET | `/stream/status` | Running state and counters |
| GET | `/events` | Event log from an offset: `after_id`, `limit`, `types=a,b` |
| GET | `/stream/events` | Server-Sent Events live push (resumable via `Last-Event-ID` / `after_id`) |

### Batch pulls

```bash
# full CSV
curl -o orders.csv "http://localhost:8000/api/v1/export/orders.csv"

# paginate JSON: follow next_cursor until it is null
curl "http://localhost:8000/api/v1/data/orders?limit=5000"
curl "http://localhost:8000/api/v1/data/orders?limit=5000&cursor=5000"

# incremental: only rows changed since your last watermark
curl -o orders_delta.csv "http://localhost:8000/api/v1/export/orders.csv?updated_since=2026-11-13T04:20:00Z"
```

For incremental loads, save the `watermark` from the first page of a pull (or the
`X-Watermark` header of a CSV export) and pass it as the next run's `updated_since`.
The watermark is 5 seconds behind the server clock, so a row committed while you were
reading is sent again next time rather than missed. `updated_since` accepts any ISO-8601
form (`2026-11-13`, `2026-11-13T10:00:00Z`, `…+05:30`). Naive values are treated as UTC.

Rows change in place (an order goes created → approved → shipped …), and the watermark
overlap re-sends some rows, so expect the same key again in later pulls. Your pipeline
should upsert or MERGE them.

### Streaming

```bash
curl -N "http://localhost:8000/api/v1/stream/events"                 # push (SSE)
curl "http://localhost:8000/api/v1/events?after_id=0&limit=500"      # pull from an offset
```

Event shape:

```json
{"event_id": 203, "event_type": "order_created", "entity": "order",
 "entity_id": "83065d6a…", "event_time": "2026-11-13T04:20:46.41Z", "payload": {...}}
```

Event types: `customer_created`, `order_created` (payload includes `items`),
`payment_received`, `order_approved`, `order_shipped`, `order_delivered`,
`order_canceled`, `review_submitted`, `customer_address_changed` (with `before`/`after`,
useful for SCD Type 2).

`event_id` is strictly increasing. Store the last one you processed to resume without gaps.
To forward events into Kinesis or MSK, run a small consumer that reads `/events` or
`/stream/events` and calls `PutRecords`, using `entity_id` as the partition key.

## How the simulator works

Each new order creates a new `customers` row (Olist issues one `customer_id` per order;
85% reuse an existing `customer_unique_id` as repeat buyers), 1–4 `order_items` sampled
from real product/seller/price rows, and one `order_payments` row. Each order then moves
through its lifecycle over about 30–90 seconds:
`created → approved → shipped → delivered → review` (about 3% get canceled instead).
Some existing customers also change address over time.

Table writes and their events are committed in the same transaction, so the tables and
the event log never disagree. The new rows use real wall-clock timestamps (2026), while
the historical data covers 2016–2018.
