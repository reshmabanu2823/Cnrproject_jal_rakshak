import asyncio
import csv
import io
import json
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import alerts as alert_store
import analytics
import forecast as fc
from config import config
from database import audit, connect, db, get_settings, init_db, put_setting, schema_version
from schemas import (AlertAction, DeviceIn, DevicePatch, IngestIn, ReadingIn, SettingsIn, WebhookIn, ZoneIn,
                     check_not_future)
from security import hash_key, new_device_key, require_admin, require_device
from seed import seed

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("jalrakshak")


# ───────────────────────── lifecycle ─────────────────────────

def scan_and_notify() -> int:
    with connect() as conn:
        events = alert_store.sync(conn)
    if not events:
        return 0
    with connect() as conn:
        return alert_store.notify(conn, events)


async def _alert_loop() -> None:
    while True:
        await asyncio.sleep(config.alert_scan_seconds)
        try:
            sent = await run_in_threadpool(scan_and_notify)
            if sent:
                log.info("alert scan sent %d notification(s)", sent)
        except Exception:
            log.exception("alert scan failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    version = init_db()
    log.info("database %s at schema v%d", config.db_path, version)
    if not config.admin_api_key:
        log.warning("JALRAKSHAK_ADMIN_KEY is not set: write endpoints are OPEN. Set it before exposing this server.")
    if config.demo_mode:
        with connect() as conn:
            if conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"] == 0:
                log.info("seeded %d demo readings", seed())
    task = asyncio.create_task(_alert_loop()) if config.alert_scan_seconds > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="JalRakshak API", version="2.0.0", lifespan=lifespan,
              description="Smart water conservation & monitoring system. "
                          "Write endpoints need `X-API-Key` when JALRAKSHAK_ADMIN_KEY is set; "
                          "sensors use `X-Device-Key` on `/api/ingest`.")

if config.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=config.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["X-Total-Count", "X-Request-ID"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error rid=%s %s %s", rid, request.method, request.url.path)
        response = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
    ms = (time.perf_counter() - t0) * 1000
    response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api"):
        log.info("%s %s %d %.1fms rid=%s", request.method, request.url.path, response.status_code, ms, rid)
    return response


Conn = Depends(db)
Admin = Depends(require_admin)


# ───────────────────────── health ─────────────────────────

@app.get("/healthz", tags=["health"])
def healthz():
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
def readyz(conn: sqlite3.Connection = Conn):
    """Readiness: the database answers and is migrated."""
    try:
        conn.execute("SELECT 1").fetchone()
        last = conn.execute("SELECT MAX(ts) t FROM readings").fetchone()["t"]
        return {"status": "ready", "schema_version": schema_version(conn), "latest_reading": last}
    except sqlite3.Error as e:
        raise HTTPException(503, f"database unavailable: {e}")


# ───────────────────────── zones ─────────────────────────

@app.get("/api/zones", tags=["zones"])
def zones(conn: sqlite3.Connection = Conn):
    return [dict(r) for r in conn.execute("SELECT id, name, icon FROM zones ORDER BY id")]


@app.post("/api/zones", status_code=201, tags=["zones"])
def add_zone(z: ZoneIn, conn: sqlite3.Connection = Conn, actor: str = Admin):
    try:
        cur = conn.execute("INSERT INTO zones(name, icon) VALUES (?, ?)", (z.name.strip(), z.icon))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A zone with that name already exists")
    audit(conn, actor, "zone.create", z.model_dump())
    return {"id": cur.lastrowid, "name": z.name.strip(), "icon": z.icon}


def _zone_exists(conn, zone_id: int) -> None:
    if not conn.execute("SELECT 1 FROM zones WHERE id=?", (zone_id,)).fetchone():
        raise HTTPException(404, "Unknown zone")


# ───────────────────────── readings ─────────────────────────

@app.post("/api/readings", status_code=201, tags=["readings"])
def add_reading(r: ReadingIn, conn: sqlite3.Connection = Conn, _: str = Admin):
    ts = r.ts or datetime.now().replace(microsecond=0)
    _zone_exists(conn, r.zone_id)
    cur = conn.execute("INSERT INTO readings(zone_id, ts, liters, source) VALUES (?,?,?, 'manual')",
                       (r.zone_id, ts.isoformat(), r.liters))
    return {"id": cur.lastrowid, "zone_id": r.zone_id, "ts": ts.isoformat(), "liters": r.liters}


def _reading_filters(zone_id, start, end, source):
    where, args = [], []
    if zone_id is not None:
        where.append("r.zone_id = ?"); args.append(zone_id)
    if start is not None:
        where.append("r.ts >= ?"); args.append(start.replace(tzinfo=None).isoformat(timespec="seconds"))
    if end is not None:
        where.append("r.ts < ?"); args.append(end.replace(tzinfo=None).isoformat(timespec="seconds"))
    if source is not None:
        where.append("r.source = ?"); args.append(source)
    return (" WHERE " + " AND ".join(where)) if where else "", args


Source = Literal["manual", "device", "import", "demo"]


@app.get("/api/readings", tags=["readings"])
def list_readings(response: Response, limit: int = Query(20, ge=1, le=1000), offset: int = Query(0, ge=0),
                  zone_id: int | None = None, start: datetime | None = None, end: datetime | None = None,
                  source: Source | None = None, conn: sqlite3.Connection = Conn):
    """Newest first. Total matching rows is in the `X-Total-Count` header."""
    where, args = _reading_filters(zone_id, start, end, source)
    total = conn.execute(f"SELECT COUNT(*) c FROM readings r{where}", args).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT r.id, z.name AS zone, z.icon, r.ts, r.liters, r.source, r.device_id FROM readings r
            JOIN zones z ON z.id=r.zone_id{where} ORDER BY r.ts DESC, r.id DESC LIMIT ? OFFSET ?""",
        (*args, limit, offset),
    ).fetchall()
    response.headers["X-Total-Count"] = str(total)
    return [dict(r) for r in rows]


@app.get("/api/readings/export.csv", tags=["readings"])
def export_readings(zone_id: int | None = None, start: datetime | None = None, end: datetime | None = None,
                    source: Source | None = None, conn: sqlite3.Connection = Conn):
    where, args = _reading_filters(zone_id, start, end, source)
    rows = conn.execute(
        f"""SELECT r.id, r.ts, z.name AS zone, r.liters, r.source, r.device_id FROM readings r
            JOIN zones z ON z.id=r.zone_id{where} ORDER BY r.ts, r.id""", args
    ).fetchall()

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "ts", "zone", "liters", "source", "device_id"])
        for i, r in enumerate(rows, 1):
            w.writerow([r["id"], r["ts"], r["zone"], r["liters"], r["source"], r["device_id"] or ""])
            if i % 1000 == 0:
                yield buf.getvalue(); buf.seek(0); buf.truncate()
        yield buf.getvalue()

    name = f"jalrakshak-readings-{datetime.now():%Y%m%d-%H%M}.csv"
    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/readings/import", tags=["readings"])
async def import_readings(request: Request, actor: str = Admin):
    """Bulk import from a utility meter or spreadsheet. Body: raw CSV (`Content-Type: text/csv`)
    with header `ts,zone,liters`. `zone` is a zone name or id. All-or-nothing: any bad row rejects the file."""
    raw = await request.body()
    if len(raw) > config.max_import_bytes:
        raise HTTPException(413, f"File too large (max {config.max_import_bytes} bytes)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV must be UTF-8")
    return await run_in_threadpool(_import_csv, text, actor)


def _import_csv(text: str, actor: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"ts", "zone", "liters"} <= {f.strip().lower() for f in reader.fieldnames}:
        raise HTTPException(422, "CSV header must include: ts, zone, liters")
    with connect() as conn:
        zones = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM zones")}
        ids = set(zones.values())
        rows, errors = [], []
        for n, rec in enumerate(reader, start=2):  # line 1 is the header
            rec = {(k or "").strip().lower(): (v or "").strip() for k, v in rec.items()}
            try:
                z = rec["zone"]
                zone_id = int(z) if z.isdigit() else zones.get(z.lower())
                if zone_id not in ids:
                    raise ValueError(f"unknown zone '{z}'")
                liters = float(rec["liters"])
                if not 0 <= liters <= 5000:
                    raise ValueError("liters must be between 0 and 5000")
                ts = check_not_future(datetime.fromisoformat(rec["ts"]))
                rows.append((zone_id, ts.isoformat(), liters))
            except (ValueError, KeyError) as e:
                errors.append({"line": n, "error": str(e)})
                if len(errors) >= 20:
                    break
        if errors:
            raise HTTPException(422, {"message": "No rows imported", "errors": errors})
        if not rows:
            raise HTTPException(422, "CSV has no data rows")
        conn.executemany("INSERT INTO readings(zone_id, ts, liters, source) VALUES (?,?,?, 'import')", rows)
        audit(conn, actor, "readings.import", {"rows": len(rows)})
    return {"imported": len(rows)}


@app.delete("/api/readings/{reading_id}", status_code=204, tags=["readings"])
def delete_reading(reading_id: int, conn: sqlite3.Connection = Conn, actor: str = Admin):
    """Remove a wrong entry (e.g. a typo in a manual reading)."""
    row = conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Reading not found")
    conn.execute("DELETE FROM readings WHERE id=?", (reading_id,))
    audit(conn, actor, "reading.delete", dict(row))
    return Response(status_code=204)


# ───────────────────────── devices (IoT) ─────────────────────────

def _device_out(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys() if k != "key_hash"}
    d["active"] = bool(d["active"])
    if r["last_seen"] is None:
        d["status"] = "never_seen"
    else:
        silent = (datetime.now() - datetime.fromisoformat(r["last_seen"])).total_seconds() / 60
        d["status"] = "offline" if silent > config.device_offline_minutes else "online"
    return d


@app.post("/api/devices", status_code=201, tags=["devices"])
def create_device(d: DeviceIn, conn: sqlite3.Connection = Conn, actor: str = Admin):
    """Register a flow sensor. The returned `device_key` is shown ONCE; flash it into the firmware."""
    _zone_exists(conn, d.zone_id)
    key = new_device_key()
    cur = conn.execute(
        "INSERT INTO devices(name, zone_id, key_hash, key_prefix, pulses_per_liter, created_at) VALUES (?,?,?,?,?,?)",
        (d.name, d.zone_id, hash_key(key), key[:8], d.pulses_per_liter, datetime.now().isoformat(timespec="seconds")),
    )
    audit(conn, actor, "device.create", {"id": cur.lastrowid, **d.model_dump()})
    row = conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone()
    return {**_device_out(row), "device_key": key}


@app.get("/api/devices", tags=["devices"])
def list_devices(conn: sqlite3.Connection = Conn, _: str = Admin):
    return [_device_out(r) for r in conn.execute("SELECT * FROM devices ORDER BY id")]


@app.patch("/api/devices/{device_id}", tags=["devices"])
def update_device(device_id: int, p: DevicePatch, conn: sqlite3.Connection = Conn, actor: str = Admin):
    if not conn.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
        raise HTTPException(404, "Device not found")
    changes = p.model_dump(exclude_none=True)
    if "zone_id" in changes:
        _zone_exists(conn, changes["zone_id"])
    if "active" in changes:
        changes["active"] = int(changes["active"])
    if changes:
        cols = ", ".join(f"{k}=?" for k in changes)  # keys come from the pydantic model, not the client
        conn.execute(f"UPDATE devices SET {cols} WHERE id=?", (*changes.values(), device_id))
        audit(conn, actor, "device.update", {"id": device_id, **changes})
    return _device_out(conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())


@app.post("/api/devices/{device_id}/rotate-key", tags=["devices"])
def rotate_device_key(device_id: int, conn: sqlite3.Connection = Conn, actor: str = Admin):
    """Issue a new key (e.g. a device was lost). The old key stops working immediately."""
    if not conn.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
        raise HTTPException(404, "Device not found")
    key = new_device_key()
    conn.execute("UPDATE devices SET key_hash=?, key_prefix=? WHERE id=?", (hash_key(key), key[:8], device_id))
    audit(conn, actor, "device.rotate_key", {"id": device_id})
    return {"id": device_id, "device_key": key}


@app.post("/api/ingest", tags=["devices"])
def ingest(body: IngestIn, device: sqlite3.Row = Depends(require_device), conn: sqlite3.Connection = Conn):
    """Endpoint for flow sensors (e.g. ESP32 + YF-S201). Send a batch of readings with `X-Device-Key`.
    Retries are safe when each reading carries a `seq`: duplicates are ignored, not double-counted."""
    now = datetime.now().replace(microsecond=0)
    accepted = duplicates = 0
    rejected = []
    max_seq = device["last_seq"]
    for i, item in enumerate(body.readings):
        try:
            ts = check_not_future(item.ts) or now
        except ValueError as e:
            rejected.append({"index": i, "seq": item.seq, "reason": str(e)})
            continue
        liters = item.liters if item.liters is not None else item.pulses / device["pulses_per_liter"]
        if liters > 5000:
            rejected.append({"index": i, "seq": item.seq, "reason": "more than 5000 L in one reading"})
            continue
        if item.seq is not None:
            max_seq = item.seq if max_seq is None else max(max_seq, item.seq)
        if liters <= 0:
            accepted += 1  # heartbeat: keeps the device "online" without storing an empty row
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO readings(zone_id, ts, liters, source, device_id, device_seq)
               VALUES (?,?,?, 'device', ?, ?)""",
            (device["zone_id"], ts.isoformat(), round(liters, 3), device["id"], item.seq),
        )
        if cur.rowcount:
            accepted += 1
        else:
            duplicates += 1
    conn.execute("UPDATE devices SET last_seen=?, last_seq=?, firmware=COALESCE(?, firmware) WHERE id=?",
                 (now.isoformat(), max_seq, body.firmware, device["id"]))
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "server_time": now.isoformat()}


# ───────────────────────── analytics ─────────────────────────

@app.get("/api/summary", tags=["analytics"])
def summary(conn: sqlite3.Connection = Conn):
    return analytics.summary(conn)


@app.get("/api/usage/daily", tags=["analytics"])
def usage_daily(days: int = 14, conn: sqlite3.Connection = Conn):
    return analytics.daily_totals(conn, max(1, min(days, 90)))


@app.get("/api/usage/hourly", tags=["analytics"])
def usage_hourly(conn: sqlite3.Connection = Conn):
    return analytics.hourly_today(conn)


@app.get("/api/usage/zones", tags=["analytics"])
def usage_zones(days: int = 7, conn: sqlite3.Connection = Conn):
    return analytics.zone_breakdown(conn, max(1, min(days, 90)))


@app.get("/api/recommendations", tags=["analytics"])
def recommendations(conn: sqlite3.Connection = Conn):
    return analytics.recommendations(conn)


@app.get("/api/forecast", tags=["analytics"])
def get_forecast(conn: sqlite3.Connection = Conn):
    """Projected usage for today and this month, plus the estimated water bill."""
    return fc.forecast(conn)


@app.get("/api/bill/estimate", tags=["analytics"])
def bill_estimate(liters: float = Query(ge=0, le=10_000_000), conn: sqlite3.Connection = Conn):
    """What a given monthly consumption would cost under the configured tariff."""
    s = get_settings(conn)
    return {**fc.bill(liters / 1000, s["tariff_slabs"], s["fixed_charge"]), "currency": s["currency"]}


@app.get("/api/reports/monthly", tags=["analytics"])
def monthly_report(month: str | None = Query(None, description="YYYY-MM; defaults to the latest data's month"),
                   conn: sqlite3.Connection = Conn):
    month = month or f"{analytics.reference_time(conn):%Y-%m}"
    if not fc.valid_month(month):
        raise HTTPException(422, "month must be YYYY-MM")
    return fc.monthly_report(conn, month)


# ───────────────────────── alerts ─────────────────────────

def _notify_in_background(events: list[dict]) -> None:
    with connect() as conn:
        alert_store.notify(conn, events)


@app.get("/api/alerts", tags=["alerts"])
def get_alerts(background: BackgroundTasks,
               status: Literal["active", "open", "acknowledged", "resolved", "all"] = "active",
               limit: int = Query(100, ge=1, le=500), conn: sqlite3.Connection = Conn):
    """Alerts with lifecycle state. `active` (default) = open + acknowledged."""
    events = alert_store.sync(conn)
    if events:
        background.add_task(_notify_in_background, events)
    if status == "active":
        return [a for a in alert_store.list_alerts(conn, None, limit) if a["status"] != "resolved"]
    return alert_store.list_alerts(conn, None if status == "all" else status, limit)


def _transition(alert_id: int, status: str, body: AlertAction | None, conn, actor: str):
    a = alert_store.transition(conn, alert_id, status, body.note if body else None, actor)
    if a is None:
        raise HTTPException(404, "Alert not found")
    audit(conn, actor, f"alert.{status}", {"id": alert_id, "note": body.note if body else None})
    return a


@app.post("/api/alerts/{alert_id}/acknowledge", tags=["alerts"])
def ack_alert(alert_id: int, body: AlertAction | None = None, conn: sqlite3.Connection = Conn, actor: str = Admin):
    """'I've seen it and I'm on it' - stays visible but is marked as handled."""
    return _transition(alert_id, "acknowledged", body, conn, actor)


@app.post("/api/alerts/{alert_id}/resolve", tags=["alerts"])
def resolve_alert(alert_id: int, body: AlertAction | None = None, conn: sqlite3.Connection = Conn,
                  actor: str = Admin):
    """Mark fixed. It re-opens automatically if the problem shows up again later."""
    return _transition(alert_id, "resolved", body, conn, actor)


@app.post("/api/alerts/{alert_id}/reopen", tags=["alerts"])
def reopen_alert(alert_id: int, conn: sqlite3.Connection = Conn, actor: str = Admin):
    return _transition(alert_id, "open", None, conn, actor)


# ───────────────────────── notifications ─────────────────────────

def _webhook_out(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["has_secret"] = bool(d.pop("secret"))
    d["active"] = bool(d["active"])
    return d


@app.get("/api/webhooks", tags=["notifications"])
def list_webhooks(conn: sqlite3.Connection = Conn, _: str = Admin):
    return [_webhook_out(r) for r in conn.execute("SELECT * FROM webhooks ORDER BY id")]


@app.post("/api/webhooks", status_code=201, tags=["notifications"])
def create_webhook(w: WebhookIn, conn: sqlite3.Connection = Conn, actor: str = Admin):
    """Push alerts to any HTTP endpoint (SMS/WhatsApp gateway, Slack, Zapier, n8n...).
    With a `secret`, each POST carries `X-JalRakshak-Signature: sha256=<hmac of body>`."""
    cur = conn.execute("INSERT INTO webhooks(url, secret, min_severity, created_at) VALUES (?,?,?,?)",
                       (str(w.url), w.secret, w.min_severity, datetime.now().isoformat(timespec="seconds")))
    audit(conn, actor, "webhook.create", {"id": cur.lastrowid, "url": str(w.url)})
    return _webhook_out(conn.execute("SELECT * FROM webhooks WHERE id=?", (cur.lastrowid,)).fetchone())


@app.delete("/api/webhooks/{webhook_id}", status_code=204, tags=["notifications"])
def delete_webhook(webhook_id: int, conn: sqlite3.Connection = Conn, actor: str = Admin):
    if not conn.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,)).rowcount:
        raise HTTPException(404, "Webhook not found")
    audit(conn, actor, "webhook.delete", {"id": webhook_id})
    return Response(status_code=204)


@app.post("/api/webhooks/{webhook_id}/test", tags=["notifications"])
def test_webhook(webhook_id: int, conn: sqlite3.Connection = Conn, _: str = Admin):
    h = conn.execute("SELECT * FROM webhooks WHERE id=?", (webhook_id,)).fetchone()
    if not h:
        raise HTTPException(404, "Webhook not found")
    status, error = alert_store.deliver(conn, h, {
        "event": "test", "id": None, "fingerprint": "test", "type": "test", "severity": "low", "zone": None,
        "title": "JalRakshak test notification", "detail": "If you can read this, alerts will reach you.",
    })
    return {"ok": error is None, "status_code": status, "error": error}


@app.get("/api/notifications", tags=["notifications"])
def notification_log(limit: int = Query(50, ge=1, le=500), conn: sqlite3.Connection = Conn, _: str = Admin):
    return [dict(r) for r in conn.execute("SELECT * FROM notification_log ORDER BY id DESC LIMIT ?", (limit,))]


# ───────────────────────── settings & admin ─────────────────────────

@app.get("/api/settings", tags=["settings"])
def read_settings(conn: sqlite3.Connection = Conn):
    return get_settings(conn)


@app.put("/api/settings", tags=["settings"])
def update_settings(s: SettingsIn, conn: sqlite3.Connection = Conn, actor: str = Admin):
    # Drop only top-level Nones: a slab's `upto_kl: null` is meaningful and must be kept.
    changes = {k: v for k, v in s.model_dump().items() if v is not None}
    for k, v in changes.items():
        put_setting(conn, k, v)
    if changes:
        audit(conn, actor, "settings.update", changes)
    return get_settings(conn)


@app.get("/api/audit", tags=["settings"])
def audit_log(limit: int = Query(100, ge=1, le=1000), conn: sqlite3.Connection = Conn, _: str = Admin):
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]


@app.post("/api/demo/reseed", tags=["settings"])
def reseed(actor: str = Admin):
    if not config.demo_mode:
        raise HTTPException(403, "Demo mode is off (JALRAKSHAK_DEMO_MODE=false)")
    n = seed()
    with connect() as conn:
        conn.execute("DELETE FROM alerts")  # demo data changed; stale alert history would be misleading
        audit(conn, actor, "demo.reseed", {"readings": n})
    return {"readings": n}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND), name="static")
