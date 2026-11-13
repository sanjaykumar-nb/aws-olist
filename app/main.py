"""Olist source-system API.

    uvicorn app.main:app --port 8000
    UI:   http://localhost:8000/
    Docs: http://localhost:8000/docs
"""

import asyncio
import csv
import hmac
import io
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .db import (
    AUDIT_COLUMN,
    DATASETS,
    column_names,
    connect,
    normalize_ts,
    now_iso,
    session,
    watermark_iso,
)
from .loader import ensure_loaded
from .simulator import simulator

log = logging.getLogger("olist")
STATIC = config.ROOT / "app" / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(ensure_loaded, log=lambda m: print(m, flush=True))
    if config.AUTOSTART:
        simulator.start()
        print(f"Stream autostarted at {simulator.rate} orders/sec", flush=True)
    if config.API_KEY:
        print("API key required for /api/v1 (except /health)", flush=True)
    yield
    simulator.stop()


app = FastAPI(
    title="Olist Source API",
    version="1.0.0",
    description="Source system for the Olist e-commerce dataset: paginated data, CSV export, "
                "and a simulated live event stream.",
    lifespan=lifespan,
)
API = "/api/v1"
OPEN_PATHS = {f"{API}/health"}


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """If OLIST_API_KEY is set, protect the API. The UI shell and /docs stay public;
    they contain no data."""
    key = config.API_KEY
    path = request.url.path
    if key and path.startswith(API) and path not in OPEN_PATHS:
        given = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
        if not hmac.compare_digest(given.encode(), key.encode()):
            return JSONResponse({"detail": "Missing or invalid API key"}, status_code=401,
                                headers={"WWW-Authenticate": "ApiKey"})
    return await call_next(request)


@app.get(f"{API}/auth", tags=["meta"], include_in_schema=False)
def auth_check():
    """200 if the caller's key is accepted (used by the UI to validate a key)."""
    return {"ok": True}


# ---------------------------------------------------------------- helpers
def _dataset_or_404(name: str) -> dict:
    if name not in DATASETS:
        raise HTTPException(404, f"Unknown dataset '{name}'. Options: {', '.join(DATASETS)}")
    return DATASETS[name]


def _where(name: str, request: Request, updated_since: str | None, cursor: int | None):
    """Build WHERE clause from updated_since, cursor, and ?<column>=<value> equality filters."""
    clauses, params = [], []
    if updated_since:
        try:
            since = normalize_ts(updated_since)
        except ValueError:
            raise HTTPException(
                422, f"updated_since must be an ISO-8601 timestamp, got '{updated_since}'"
            ) from None
        clauses.append(f'"{AUDIT_COLUMN}" > ?')
        params.append(since)
    if cursor is not None:
        clauses.append("rowid > ?")
        params.append(cursor)
    cols = set(column_names(name))
    for key, value in request.query_params.items():
        if key in cols:
            clauses.append(f'"{key}" = ?')
            params.append(value)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


# ---------------------------------------------------------------- meta
@app.get(f"{API}/health", tags=["meta"])
def health():
    with session() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "server_time": now_iso(), "auth_required": bool(config.API_KEY)}


@app.get(f"{API}/datasets", tags=["meta"])
def list_datasets():
    out = []
    with session() as conn:
        loaded = {
            k.removeprefix("rows:"): int(v)
            for k, v in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'rows:%'")
        }
        for name, spec in DATASETS.items():
            count = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            last = conn.execute(f'SELECT max("{AUDIT_COLUMN}") FROM "{name}"').fetchone()[0]
            base = loaded.get(name, count)
            out.append({
                "name": name,
                "source_file": spec["file"],
                "row_count": count,
                "loaded_row_count": base,
                "simulated_row_count": count - base,
                "last_updated_at": last,
                "columns": [{"name": c, "type": t} for c, t in spec["columns"]]
                + [{"name": AUDIT_COLUMN, "type": "TEXT"}],
            })
    return {"datasets": out}


# ---------------------------------------------------------------- data (JSON, paginated)
@app.get(f"{API}/data/{{dataset}}", tags=["data"])
def get_data(
    dataset: str,
    request: Request,
    limit: int = Query(config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    cursor: int | None = Query(None, description="next_cursor from the previous page"),
    updated_since: str | None = Query(
        None, description="Only rows with updated_at greater than this (ISO, e.g. 2026-11-13T10:00:00Z)"
    ),
):
    """Keyset-paginated rows. Any column can be used as an equality filter,
    e.g. `/data/orders?order_status=delivered`."""
    _dataset_or_404(dataset)
    watermark = watermark_iso()  # taken before reading: use as the next run's updated_since
    where, params = _where(dataset, request, updated_since, cursor)
    sql = f'SELECT rowid AS _rowid, * FROM "{dataset}"{where} ORDER BY rowid LIMIT ?'
    with session() as conn:
        rows = conn.execute(sql, [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    data = [{k: r[k] for k in r.keys() if k != "_rowid"} for r in rows]
    return {
        "dataset": dataset,
        "count": len(data),
        "next_cursor": rows[-1]["_rowid"] if has_more else None,
        "watermark": watermark,
        "data": data,
    }


# ---------------------------------------------------------------- CSV export (streamed)
@app.get(f"{API}/export/{{dataset}}.csv", tags=["export"])
def export_csv(
    dataset: str,
    request: Request,
    updated_since: str | None = Query(None, description="Incremental export: rows changed after this"),
):
    """Full or incremental CSV download. Supports the same column filters as /data."""
    _dataset_or_404(dataset)
    where, params = _where(dataset, request, updated_since, None)
    cols = column_names(dataset)
    watermark = watermark_iso()

    def generate():
        conn = connect()
        try:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(cols)
            cur = conn.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} '
                               f'FROM "{dataset}"{where} ORDER BY rowid', params)
            while True:
                chunk = cur.fetchmany(5000)
                if not chunk:
                    break
                writer.writerows(tuple(r) for r in chunk)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        finally:
            conn.close()

    suffix = "_incremental" if updated_since else ""
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{dataset}{suffix}.csv"',
            "X-Watermark": watermark,
            "Access-Control-Expose-Headers": "X-Watermark",
        },
    )


# ---------------------------------------------------------------- stream control
class StartRequest(BaseModel):
    orders_per_sec: float | None = Field(None, gt=0, le=config.MAX_ORDERS_PER_SEC)


@app.post(f"{API}/stream/start", tags=["stream"])
def stream_start(body: StartRequest | None = None):
    simulator.start(body.orders_per_sec if body else None)
    return simulator.status()


@app.post(f"{API}/stream/stop", tags=["stream"])
def stream_stop():
    simulator.stop()
    return simulator.status()


@app.post(f"{API}/stream/rate", tags=["stream"])
def stream_rate(body: StartRequest):
    if body.orders_per_sec is None:
        raise HTTPException(422, "orders_per_sec is required")
    simulator.set_rate(body.orders_per_sec)
    return simulator.status()


@app.get(f"{API}/stream/status", tags=["stream"])
def stream_status():
    status = simulator.status()
    with session() as conn:
        status["total_events"] = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        status["last_event_id"] = conn.execute("SELECT max(event_id) FROM events").fetchone()[0]
        status["inflight_orders"] = conn.execute("SELECT count(*) FROM sim_inflight").fetchone()[0]
    return status


# ---------------------------------------------------------------- events (pull + push)
def _fetch_events(after_id: int, limit: int, types: list[str] | None):
    sql = "SELECT * FROM events WHERE event_id > ?"
    params: list = [after_id]
    if types:
        sql += f" AND event_type IN ({','.join('?' * len(types))})"
        params += types
    sql += " ORDER BY event_id LIMIT ?"
    params.append(limit)
    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def _types(types: str | None) -> list[str] | None:
    return [t.strip() for t in types.split(",") if t.strip()] if types else None


@app.get(f"{API}/events", tags=["events"])
def get_events(
    after_id: int = Query(0, ge=0, description="Return events with event_id greater than this"),
    limit: int = Query(500, ge=1, le=5000),
    types: str | None = Query(None, description="Comma-separated event types"),
):
    """Pull-based event log (like reading a Kafka/Kinesis stream from an offset)."""
    events = _fetch_events(after_id, limit, _types(types))
    return {
        "count": len(events),
        "next_after_id": events[-1]["event_id"] if events else after_id,
        "events": events,
    }


@app.get(f"{API}/stream/events", tags=["events"])
async def sse_events(
    request: Request,
    after_id: int | None = Query(None, description="Resume from here; default = only new events"),
    types: str | None = Query(None),
):
    """Server-Sent Events push feed. Each message's `id` is the event_id (resumable)."""
    type_list = _types(types)
    resume = request.headers.get("last-event-id")  # sent automatically by EventSource on reconnect
    if resume and resume.isdigit():
        after_id = int(resume)
    if after_id is None:
        with session() as conn:
            after_id = conn.execute("SELECT coalesce(max(event_id), 0) FROM events").fetchone()[0]

    async def gen():
        last = after_id
        idle = 0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            events = await asyncio.to_thread(_fetch_events, last, 500, type_list)
            for e in events:
                last = e["event_id"]
                yield f"id: {last}\ndata: {json.dumps(e)}\n\n"
            if not events:
                idle += 1
                if idle % 30 == 0:  # comment line every ~15s keeps proxies from closing it
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)
            else:
                idle = 0

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- frontend
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
