"""Authentication and rate limiting.

* Admin key  - `X-API-Key: <key>` or `Authorization: Bearer <key>` on write/admin
  endpoints. If JALRAKSHAK_ADMIN_KEY is unset the API runs open (dev mode).
* Device key - `X-Device-Key: <key>` on /api/ingest. Each sensor gets its own key,
  stored only as a SHA-256 hash, and can be revoked on its own.
* Rate limit - in-memory token bucket. Fine for one process; use a shared store
  (Redis) or a reverse-proxy limit if you run several workers.
"""
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

from fastapi import Depends, Header, HTTPException, Request

from config import config
from database import db

DEVICE_KEY_PREFIX = "jr_"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_device_key() -> str:
    return DEVICE_KEY_PREFIX + secrets.token_urlsafe(24)


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, per_minute: int) -> bool:
        if per_minute <= 0:
            return True
        now = time.monotonic()
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1, now)
            if len(self._buckets) > 10_000:  # crude memory bound
                self._buckets.clear()
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


limiter = RateLimiter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limited(request: Request) -> None:
    if not limiter.allow("ip:" + _client_ip(request), config.rate_limit_per_min):
        raise HTTPException(429, "Too many requests, slow down", headers={"Retry-After": "10"})


def _extract_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def require_admin(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    """Returns the actor name for the audit log."""
    rate_limited(request)
    expected = config.admin_api_key
    if not expected:
        return "anonymous(dev)"
    given = _extract_key(x_api_key, authorization)
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
    return "admin"


def require_device(
    x_device_key: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(db),
) -> sqlite3.Row:
    if not x_device_key:
        raise HTTPException(401, "Missing X-Device-Key header")
    row = conn.execute("SELECT * FROM devices WHERE key_hash = ?", (hash_key(x_device_key),)).fetchone()
    if not row:
        raise HTTPException(401, "Unknown device key")
    if not row["active"]:
        raise HTTPException(403, "Device is disabled")
    if not limiter.allow(f"device:{row['id']}", config.device_rate_limit_per_min):
        raise HTTPException(429, "Device is sending too fast", headers={"Retry-After": "30"})
    return row
