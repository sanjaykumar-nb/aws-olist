"""Settings, read from environment variables (or a .env-free default)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATASET_DIR = Path(os.getenv("OLIST_DATASET_DIR", str(ROOT / "dataset")))
DB_PATH = Path(os.getenv("OLIST_DB_PATH", str(ROOT / "data" / "olist.db")))

# Default streaming rate (new orders per second) and the max allowed from the UI/API.
DEFAULT_ORDERS_PER_SEC = float(os.getenv("OLIST_ORDERS_PER_SEC", "1"))
MAX_ORDERS_PER_SEC = 50.0

def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# When set, every /api/v1 endpoint except /health requires this key, sent as the
# X-API-Key header (or ?api_key= for browser EventSource / download links).
API_KEY = os.getenv("OLIST_API_KEY", "").strip() or None

# Start the streaming simulator automatically when the server starts.
AUTOSTART = _bool("OLIST_AUTOSTART", False)

# Delete events older than this many hours (0 = keep forever). Keeps the disk bounded.
EVENT_RETENTION_HOURS = float(os.getenv("OLIST_EVENT_RETENTION_HOURS", "168"))

# Page size limits for the data API.
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 10_000
