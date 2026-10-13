import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "jalrakshak.db"

ZONES = [
    ("Kitchen", "🍳"),
    ("Bathroom", "🚿"),
    ("Laundry", "🧺"),
    ("Garden", "🌿"),
    ("Toilet", "🚽"),
    ("Cleaning", "🧽"),
]

SCHEMA = """
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
"""

DEFAULT_SETTINGS = {
    "household_size": "4",
    "lpcd_target": "135",  # litres per capita per day (BIS benchmark for urban domestic use)
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for name, icon in ZONES:
            conn.execute("INSERT OR IGNORE INTO zones(name, icon) VALUES (?, ?)", (name, icon))
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def get_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    s = {r["key"]: r["value"] for r in rows}
    return {"household_size": int(s["household_size"]), "lpcd_target": float(s["lpcd_target"])}
