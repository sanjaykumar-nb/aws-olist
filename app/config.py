"""Settings, read from environment variables (or a .env-free default)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATASET_DIR = Path(os.getenv("OLIST_DATASET_DIR", str(ROOT / "dataset")))
DB_PATH = Path(os.getenv("OLIST_DB_PATH", str(ROOT / "data" / "olist.db")))

# Default streaming rate (new orders per second) and the max allowed from the UI/API.
DEFAULT_ORDERS_PER_SEC = float(os.getenv("OLIST_ORDERS_PER_SEC", "1"))
MAX_ORDERS_PER_SEC = 50.0

# Page size limits for the data API.
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 10_000
