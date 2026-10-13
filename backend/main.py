from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import analytics
from database import get_conn, get_settings, init_db
from seed import seed

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="JalRakshak API", version="1.0.0",
              description="Smart water conservation & monitoring system")


@app.on_event("startup")
def startup():
    init_db()
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] == 0:
            seed()


class ReadingIn(BaseModel):
    zone_id: int
    liters: float = Field(ge=0, le=5000)
    ts: datetime | None = None


class SettingsIn(BaseModel):
    household_size: int = Field(ge=1, le=100)
    lpcd_target: float = Field(gt=0, le=1000)


@app.get("/api/zones")
def zones():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT id, name, icon FROM zones ORDER BY id")]


@app.post("/api/readings", status_code=201)
def add_reading(r: ReadingIn):
    ts = (r.ts or datetime.now()).replace(microsecond=0)
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM zones WHERE id=?", (r.zone_id,)).fetchone():
            raise HTTPException(404, "Unknown zone")
        cur = conn.execute("INSERT INTO readings(zone_id, ts, liters) VALUES (?,?,?)",
                           (r.zone_id, ts.isoformat(), r.liters))
        return {"id": cur.lastrowid, "zone_id": r.zone_id, "ts": ts.isoformat(), "liters": r.liters}


@app.get("/api/readings")
def recent_readings(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, z.name AS zone, z.icon, r.ts, r.liters FROM readings r
               JOIN zones z ON z.id=r.zone_id ORDER BY r.ts DESC, r.id DESC LIMIT ?""",
            (min(limit, 200),),
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/summary")
def summary():
    with get_conn() as conn:
        return analytics.summary(conn)


@app.get("/api/usage/daily")
def usage_daily(days: int = 14):
    with get_conn() as conn:
        return analytics.daily_totals(conn, max(1, min(days, 90)))


@app.get("/api/usage/hourly")
def usage_hourly():
    with get_conn() as conn:
        return analytics.hourly_today(conn)


@app.get("/api/usage/zones")
def usage_zones(days: int = 7):
    with get_conn() as conn:
        return analytics.zone_breakdown(conn, max(1, min(days, 90)))


@app.get("/api/alerts")
def alerts():
    with get_conn() as conn:
        return analytics.detect_anomalies(conn)


@app.get("/api/recommendations")
def recommendations():
    with get_conn() as conn:
        return analytics.recommendations(conn)


@app.get("/api/settings")
def read_settings():
    with get_conn() as conn:
        return get_settings(conn)


@app.put("/api/settings")
def update_settings(s: SettingsIn):
    with get_conn() as conn:
        conn.execute("UPDATE settings SET value=? WHERE key='household_size'", (str(s.household_size),))
        conn.execute("UPDATE settings SET value=? WHERE key='lpcd_target'", (str(s.lpcd_target),))
        return get_settings(conn)


@app.post("/api/demo/reseed")
def reseed():
    return {"readings": seed()}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND), name="static")
