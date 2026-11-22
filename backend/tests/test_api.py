import json
import sqlite3
from datetime import datetime, timedelta

import alerts as alert_store
import forecast as fc
from config import config
from database import MIGRATIONS, connect, migrate, schema_version


# ── health & compatibility ──

def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz").json()
    assert r["status"] == "ready" and r["schema_version"] == MIGRATIONS[-1][0]


def test_dashboard_endpoints_still_work(client):
    for path in ("/api/summary", "/api/usage/daily?days=14", "/api/usage/zones", "/api/usage/hourly",
                 "/api/recommendations", "/api/zones", "/api/settings", "/"):
        assert client.get(path).status_code == 200, path
    assert "X-Request-ID" in client.get("/api/summary").headers


def test_demo_data_finds_planted_problems(client):
    alerts = client.get("/api/alerts").json()
    types = {(a["type"], a["zone"]) for a in alerts}
    assert ("leak", "Toilet") in types
    assert ("spike", "Garden") in types
    assert all(a["status"] == "open" and "id" in a for a in alerts)


# ── readings ──

def test_reading_validation(client):
    assert client.post("/api/readings", json={"zone_id": 1, "liters": -1}).status_code == 422
    assert client.post("/api/readings", json={"zone_id": 999, "liters": 5}).status_code == 404
    future = (datetime.now() + timedelta(days=1)).isoformat()
    assert client.post("/api/readings", json={"zone_id": 1, "liters": 5, "ts": future}).status_code == 422
    assert client.post("/api/readings", json={"zone_id": 1, "liters": 5}).status_code == 201


def test_readings_filter_and_pagination(client):
    r = client.get("/api/readings", params={"zone_id": 4, "limit": 5, "offset": 5})
    assert r.status_code == 200 and len(r.json()) == 5
    assert int(r.headers["X-Total-Count"]) > 10
    assert {x["zone"] for x in r.json()} == {"Garden"}


def test_delete_reading(client):
    rid = client.post("/api/readings", json={"zone_id": 1, "liters": 999}).json()["id"]
    assert client.delete(f"/api/readings/{rid}").status_code == 204
    assert client.delete(f"/api/readings/{rid}").status_code == 404


def test_csv_round_trip(client):
    ts = (datetime.now() - timedelta(hours=2)).replace(microsecond=0).isoformat()
    body = f"ts,zone,liters\n{ts},Kitchen,12.5\n{ts},2,3\n"
    r = client.post("/api/readings/import", content=body, headers={"Content-Type": "text/csv"})
    assert r.json() == {"imported": 2}
    out = client.get("/api/readings/export.csv", params={"source": "import"})
    lines = out.text.strip().splitlines()
    assert lines[0] == "id,ts,zone,liters,source,device_id" and len(lines) == 3


def test_csv_import_is_all_or_nothing(client):
    body = "ts,zone,liters\n2026-01-01T10:00:00,Kitchen,5\n2026-01-01T11:00:00,Nowhere,5\n"
    r = client.post("/api/readings/import", content=body, headers={"Content-Type": "text/csv"})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["line"] == 3
    assert client.get("/api/readings", params={"source": "import"}).json() == []


# ── auth ──

def test_writes_need_admin_key_when_configured(client, admin_key):
    assert client.post("/api/readings", json={"zone_id": 1, "liters": 5}).status_code == 401
    assert client.post("/api/readings", json={"zone_id": 1, "liters": 5},
                       headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/api/readings", json={"zone_id": 1, "liters": 5}, headers=admin_key).status_code == 201
    bearer = {"Authorization": "Bearer " + admin_key["X-API-Key"]}
    assert client.put("/api/settings", json={"household_size": 3}, headers=bearer).status_code == 200
    assert client.get("/api/summary").status_code == 200  # reads stay public


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(config, "rate_limit_per_min", 3)
    codes = [client.post("/api/readings", json={"zone_id": 1, "liters": 1}).status_code for _ in range(5)]
    assert codes[:3] == [201] * 3 and codes[-1] == 429


def test_reseed_blocked_outside_demo(client, monkeypatch):
    monkeypatch.setattr(config, "demo_mode", False)
    assert client.post("/api/demo/reseed").status_code == 403


# ── devices ──

def _device(client, zone_id=5, **kw):
    r = client.post("/api/devices", json={"name": "Toilet sensor", "zone_id": zone_id, **kw})
    assert r.status_code == 201
    return r.json()


def test_device_ingest_pulses_and_dedupe(client):
    dev = _device(client, pulses_per_liter=450)
    assert dev["device_key"].startswith("jr_") and "key_hash" not in dev
    h = {"X-Device-Key": dev["device_key"]}
    batch = {"firmware": "1.2.0", "readings": [{"seq": 1, "pulses": 900}, {"seq": 2, "liters": 1.5}]}
    r = client.post("/api/ingest", json=batch, headers=h).json()
    assert r["accepted"] == 2 and r["duplicates"] == 0
    r = client.post("/api/ingest", json=batch, headers=h).json()  # network retry
    assert r["accepted"] == 0 and r["duplicates"] == 2
    rows = client.get("/api/readings", params={"source": "device"}).json()
    assert sorted(x["liters"] for x in rows) == [1.5, 2.0]
    d = client.get("/api/devices").json()[0]
    assert d["status"] == "online" and d["last_seq"] == 2 and d["firmware"] == "1.2.0"


def test_ingest_auth_and_validation(client):
    assert client.post("/api/ingest", json={"readings": [{"liters": 1}]}).status_code == 401
    assert client.post("/api/ingest", json={"readings": [{"liters": 1}]},
                       headers={"X-Device-Key": "jr_nope"}).status_code == 401
    dev = _device(client)
    h = {"X-Device-Key": dev["device_key"]}
    assert client.post("/api/ingest", json={"readings": [{"liters": 1, "pulses": 3}]}, headers=h).status_code == 422
    future = (datetime.now() + timedelta(hours=2)).isoformat()
    r = client.post("/api/ingest", json={"readings": [{"liters": 1, "ts": future}, {"liters": 2}]}, headers=h).json()
    assert r["accepted"] == 1 and r["rejected"][0]["index"] == 0


def test_disable_and_rotate_device(client):
    dev = _device(client)
    old = {"X-Device-Key": dev["device_key"]}
    new_key = client.post(f"/api/devices/{dev['id']}/rotate-key").json()["device_key"]
    assert client.post("/api/ingest", json={"readings": [{"liters": 1}]}, headers=old).status_code == 401
    new = {"X-Device-Key": new_key}
    assert client.post("/api/ingest", json={"readings": [{"liters": 1}]}, headers=new).status_code == 200
    client.patch(f"/api/devices/{dev['id']}", json={"active": False})
    assert client.post("/api/ingest", json={"readings": [{"liters": 1}]}, headers=new).status_code == 403


def test_offline_sensor_raises_alert(client):
    dev = _device(client)
    client.post("/api/ingest", json={"readings": [{"liters": 1}]}, headers={"X-Device-Key": dev["device_key"]})
    stale = (datetime.now() - timedelta(hours=8)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (stale, dev["id"]))
    off = [a for a in client.get("/api/alerts").json() if a["type"] == "offline"]
    assert len(off) == 1 and off[0]["severity"] == "high"


# ── alert lifecycle ──

def test_alert_lifecycle(client):
    leak = next(a for a in client.get("/api/alerts").json() if a["type"] == "leak")
    # repeated scans do not duplicate it
    assert sum(a["type"] == "leak" for a in client.get("/api/alerts").json()) == 1
    r = client.post(f"/api/alerts/{leak['id']}/acknowledge", json={"note": "plumber booked"}).json()
    assert r["status"] == "acknowledged" and r["note"] == "plumber booked"
    r = client.post(f"/api/alerts/{leak['id']}/resolve").json()
    assert r["status"] == "resolved" and r["resolved_by"] == "anonymous(dev)"
    # still visible in old data, but resolved by a person -> stays resolved
    assert leak["id"] not in [a["id"] for a in client.get("/api/alerts").json()]
    assert leak["id"] in [a["id"] for a in client.get("/api/alerts", params={"status": "resolved"}).json()]
    assert client.post("/api/alerts/99999/resolve").status_code == 404
    actions = [e["action"] for e in client.get("/api/audit").json()]
    assert "alert.acknowledged" in actions and "alert.resolved" in actions


def test_cleared_condition_auto_resolves(client):
    client.get("/api/alerts")
    with connect() as conn:
        conn.execute("DELETE FROM readings WHERE zone_id = 5 AND CAST(substr(ts,12,2) AS INTEGER) BETWEEN 0 AND 5")
    client.get("/api/alerts")
    leak = next(a for a in client.get("/api/alerts", params={"status": "all"}).json() if a["type"] == "leak")
    assert leak["status"] == "resolved" and leak["resolved_by"] == "auto"


def test_webhook_notification(client, monkeypatch):
    sent = []

    def fake_post(url, body, secret):
        sent.append((url, json.loads(body), alert_store.sign(secret, body) if secret else None))
        return 200

    monkeypatch.setattr(alert_store, "_post", fake_post)
    assert client.post("/api/webhooks", json={"url": "ftp://x"}).status_code == 422
    hook = client.post("/api/webhooks", json={"url": "https://example.com/hook", "secret": "topsecret1",
                                              "min_severity": "high"}).json()
    assert hook["has_secret"] and "secret" not in hook
    client.get("/api/alerts")  # background task delivers after the response
    assert sent and all(p["alert"]["severity"] == "high" for _, p, _ in sent)
    assert sent[0][2].startswith("sha256=")
    n_first = len(sent)
    client.get("/api/alerts")
    assert len(sent) == n_first  # no re-notification for the same alerts
    assert client.post(f"/api/webhooks/{hook['id']}/test").json()["ok"] is True
    assert len(client.get("/api/notifications").json()) == n_first + 1


# ── forecast & billing ──

def test_bill_slabs():
    slabs = [{"upto_kl": 8, "rate": 7}, {"upto_kl": 25, "rate": 11}, {"upto_kl": None, "rate": 26}]
    b = fc.bill(30, slabs, 50)
    assert b["usage_charge"] == 8 * 7 + 17 * 11 + 5 * 26
    assert b["total"] == b["usage_charge"] + 50
    assert fc.bill(0, slabs)["total"] == 0
    assert fc.bill(5, slabs)["slabs"] == [{"from_kl": 0.0, "to_kl": 8, "kl": 5, "rate": 7, "amount": 35}]


def test_forecast_and_report(client):
    f = client.get("/api/forecast").json()
    assert f["today"]["projected_liters"] >= f["today"]["so_far_liters"]
    assert f["month"]["projected_bill"]["total"] > 0
    rep = client.get("/api/reports/monthly").json()
    assert rep["total_liters"] > 0 and rep["bill"]["total"] > 0
    assert abs(sum(z["share"] for z in rep["zones"]) - 100) < 1
    assert client.get("/api/reports/monthly", params={"month": "2026-13"}).status_code == 422
    assert client.get("/api/bill/estimate", params={"liters": 20000}).json()["kl"] == 20


def test_settings_partial_update_and_tariff_validation(client):
    s = client.put("/api/settings", json={"household_size": 5}).json()
    assert s["household_size"] == 5 and s["lpcd_target"] == 135
    bad = {"tariff_slabs": [{"upto_kl": 10, "rate": 5}, {"upto_kl": 5, "rate": 6}, {"upto_kl": None, "rate": 9}]}
    assert client.put("/api/settings", json=bad).status_code == 422
    good = {"tariff_slabs": [{"upto_kl": 10, "rate": 5}, {"upto_kl": None, "rate": 9}], "currency": "INR"}
    assert client.put("/api/settings", json=good).json()["tariff_slabs"][1]["upto_kl"] is None


# ── migrations ──

def test_migrates_pre_migration_database(tmp_path):
    """A DB created by v1.0 (no user_version, old tables with data) upgrades in place."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(MIGRATIONS[0][1])
    conn.execute("INSERT INTO zones(name) VALUES ('Kitchen')")
    conn.execute("INSERT INTO readings(zone_id, ts, liters) VALUES (1, '2026-01-01T10:00:00', 4)")
    conn.commit()
    assert schema_version(conn) == 0
    assert migrate(conn) == MIGRATIONS[-1][0]
    row = conn.execute("SELECT source, device_id FROM readings").fetchone()
    assert row["source"] == "manual" and row["device_id"] is None
    assert migrate(conn) == MIGRATIONS[-1][0]  # idempotent
    conn.close()
