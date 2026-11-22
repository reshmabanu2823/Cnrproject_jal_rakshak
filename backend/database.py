"""SQLite access: connections, versioned schema migrations, settings.

Connections are opened per request (see `db()` FastAPI dependency) and always
closed. WAL mode lets the dashboard read while devices are writing.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from config import config

ZONES = [
    ("Kitchen", "🍳"),
    ("Bathroom", "🚿"),
    ("Laundry", "🧺"),
    ("Garden", "🌿"),
    ("Toilet", "🚽"),
    ("Cleaning", "🧽"),
]

# Ordered, append-only list of (version, sql). Never edit a shipped migration;
# add a new one. Version 1 uses IF NOT EXISTS so databases created before
# migrations existed are adopted safely.
MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE IF NOT EXISTS zones (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        icon TEXT NOT NULL DEFAULT '💧'
    );
    CREATE TABLE IF NOT EXISTS readings (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        zone_id INTEGER NOT NULL REFERENCES zones(id),
        ts      TEXT NOT NULL,          -- ISO 8601, local time, e.g. 2026-10-13T07:30:00
        liters  REAL NOT NULL CHECK (liters >= 0)
    );
    CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """),
    (2, """
    CREATE TABLE devices (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL,
        zone_id          INTEGER NOT NULL REFERENCES zones(id),
        key_hash         TEXT NOT NULL UNIQUE,      -- sha256 of the device key; the key itself is never stored
        key_prefix       TEXT NOT NULL,             -- first chars, so admins can tell keys apart
        pulses_per_liter REAL NOT NULL DEFAULT 450, -- YF-S201 is ~450 pulses/L; calibrate per sensor
        active           INTEGER NOT NULL DEFAULT 1,
        created_at       TEXT NOT NULL,
        last_seen        TEXT,
        last_seq         INTEGER,
        firmware         TEXT
    );
    ALTER TABLE readings ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';  -- manual | device | import | demo
    ALTER TABLE readings ADD COLUMN device_id INTEGER REFERENCES devices(id);
    ALTER TABLE readings ADD COLUMN device_seq INTEGER;
    -- devices retry on bad networks; (device, seq) makes ingestion idempotent
    CREATE UNIQUE INDEX idx_readings_device_seq ON readings(device_id, device_seq)
        WHERE device_id IS NOT NULL AND device_seq IS NOT NULL;
    CREATE INDEX idx_readings_zone_ts ON readings(zone_id, ts);

    CREATE TABLE alerts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        fingerprint     TEXT NOT NULL UNIQUE,  -- stable identity, so one leak = one alert, not one per scan
        type            TEXT NOT NULL,
        severity        TEXT NOT NULL,
        zone            TEXT,
        title           TEXT NOT NULL,
        detail          TEXT NOT NULL,
        data            TEXT NOT NULL DEFAULT '{}',
        status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
        first_seen      TEXT NOT NULL,
        last_seen       TEXT NOT NULL,
        acknowledged_at TEXT,
        resolved_at     TEXT,
        resolved_by     TEXT,                  -- 'auto' when the condition cleared on its own
        note            TEXT
    );
    CREATE INDEX idx_alerts_status ON alerts(status);

    CREATE TABLE webhooks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        url         TEXT NOT NULL,
        secret      TEXT,                 -- used for the HMAC signature header
        min_severity TEXT NOT NULL DEFAULT 'medium',
        active      INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL,
        last_status INTEGER,
        last_error  TEXT
    );
    CREATE TABLE notification_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        webhook_id  INTEGER REFERENCES webhooks(id) ON DELETE CASCADE,
        alert_id    INTEGER REFERENCES alerts(id) ON DELETE CASCADE,
        event       TEXT NOT NULL,
        ts          TEXT NOT NULL,
        attempts    INTEGER NOT NULL,
        status_code INTEGER,
        error       TEXT
    );
    CREATE TABLE audit_log (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     TEXT NOT NULL,
        actor  TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT
    );
    """),
]

# Example slab tariff (per kilolitre per month). Utilities differ; set yours via PUT /api/settings.
DEFAULT_TARIFF = [
    {"upto_kl": 8, "rate": 7.0},
    {"upto_kl": 25, "rate": 11.0},
    {"upto_kl": 50, "rate": 26.0},
    {"upto_kl": None, "rate": 45.0},
]

DEFAULT_SETTINGS = {
    "household_size": "4",
    "lpcd_target": "135",  # litres per capita per day (BIS benchmark for urban domestic use)
    "tariff_slabs": json.dumps(DEFAULT_TARIFF),
    "fixed_charge": "50",  # monthly meter / service charge
    "currency": "INR",
}


def get_conn() -> sqlite3.Connection:
    # check_same_thread=False: FastAPI may run a sync dependency and its endpoint on
    # different threadpool threads. Each connection is still used by one request at a time.
    conn = sqlite3.connect(config.db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, always close."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency."""
    with connect() as conn:
        yield conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        # executescript commits implicitly; wrap each migration in its own transaction.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        current = version
    return current


def init_db() -> int:
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = migrate(conn)
        for name, icon in ZONES:
            conn.execute("INSERT OR IGNORE INTO zones(name, icon) VALUES (?, ?)", (name, icon))
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        conn.commit()
        return version
    finally:
        conn.close()


def get_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    s = {**DEFAULT_SETTINGS, **{r["key"]: r["value"] for r in rows}}
    return {
        "household_size": int(s["household_size"]),
        "lpcd_target": float(s["lpcd_target"]),
        "tariff_slabs": json.loads(s["tariff_slabs"]),
        "fixed_charge": float(s["fixed_charge"]),
        "currency": s["currency"],
    }


def put_setting(conn: sqlite3.Connection, key: str, value) -> None:
    v = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, v))


def audit(conn: sqlite3.Connection, actor: str, action: str, detail=None) -> None:
    conn.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES (?,?,?,?)",
                 (datetime.now().isoformat(timespec="seconds"), actor, action,
                  json.dumps(detail, default=str) if detail is not None else None))
