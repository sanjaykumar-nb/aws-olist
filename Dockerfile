FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OLIST_DATASET_DIR=/dataset \
    OLIST_DB_PATH=/data/olist.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user; /data holds the SQLite DB (mount a volume there).
RUN useradd --system --uid 10001 --home-dir /srv olist \
 && mkdir -p /data \
 && chown olist /data
USER olist

EXPOSE 8000

# First start loads the CSVs (~15 s), hence the start period.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"

# One worker only: the stream simulator lives inside the process and SQLite has one writer.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-graceful-shutdown", "5"]
