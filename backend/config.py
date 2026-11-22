"""Runtime configuration, read from environment variables (12-factor style).

Every value has a safe local-dev default, so `uvicorn main:app` still works with
zero setup. In production set at least JALRAKSHAK_ADMIN_KEY and
JALRAKSHAK_DEMO_MODE=false. See .env.example for the full list.
"""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


class Config:
    def __init__(self) -> None:
        self.db_path = Path(os.getenv("JALRAKSHAK_DB", BACKEND_DIR / "jalrakshak.db"))
        # When unset, write endpoints are open (local dev only - a warning is logged).
        self.admin_api_key: str | None = os.getenv("JALRAKSHAK_ADMIN_KEY") or None
        self.cors_origins = [o.strip() for o in os.getenv("JALRAKSHAK_CORS_ORIGINS", "").split(",") if o.strip()]
        # Demo mode: seed sample data on an empty DB and allow /api/demo/reseed.
        self.demo_mode = _bool("JALRAKSHAK_DEMO_MODE", True)
        # Requests per minute per client IP on write endpoints, and per device on ingest.
        self.rate_limit_per_min = _int("JALRAKSHAK_RATE_LIMIT_PER_MIN", 120)
        self.device_rate_limit_per_min = _int("JALRAKSHAK_DEVICE_RATE_LIMIT_PER_MIN", 60)
        # A device that has not reported for this long raises a "sensor offline" alert.
        self.device_offline_minutes = _int("JALRAKSHAK_DEVICE_OFFLINE_MINUTES", 30)
        # Background alert scan interval; 0 disables the loop (alerts still sync on read).
        self.alert_scan_seconds = _int("JALRAKSHAK_ALERT_SCAN_SECONDS", 60)
        self.webhook_timeout_s = _float("JALRAKSHAK_WEBHOOK_TIMEOUT_S", 5.0)
        self.webhook_max_attempts = _int("JALRAKSHAK_WEBHOOK_MAX_ATTEMPTS", 3)
        self.log_level = os.getenv("JALRAKSHAK_LOG_LEVEL", "INFO").upper()
        self.max_import_bytes = _int("JALRAKSHAK_MAX_IMPORT_BYTES", 5 * 1024 * 1024)


config = Config()
