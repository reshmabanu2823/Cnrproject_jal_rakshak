"""Usage analytics: summaries, anomaly / leak detection, recommendations.

All logic is rule-based + simple statistics (no heavy ML dependency), so it is
explainable in a viva: every alert says *why* it fired.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, pstdev

from config import config
from database import get_settings

NIGHT_HOURS = (1, 2, 3, 4)       # a household is normally idle here
MIN_NIGHT_FLOW_L = 1.0           # litres/hour that counts as "water is flowing"
SPIKE_SIGMA = 3.0                # z-score threshold
MIN_SPIKE_L = 25.0               # ignore tiny absolute spikes
BASELINE_MIN_SAMPLES = 5


def reference_time(conn: sqlite3.Connection) -> datetime:
    """'Now' = latest reading, so the demo works on any date and with replayed data."""
    row = conn.execute("SELECT MAX(ts) AS t FROM readings").fetchone()
    return datetime.fromisoformat(row["t"]) if row["t"] else datetime.now()


def _hourly(conn, since: datetime):
    """{zone_name: {datetime(hour): litres}}"""
    q = """SELECT z.name AS zone, substr(r.ts, 1, 13) AS h, SUM(r.liters) AS l
           FROM readings r JOIN zones z ON z.id = r.zone_id
           WHERE r.ts >= ? GROUP BY z.name, h"""
    out = defaultdict(dict)
    for r in conn.execute(q, (since.isoformat(timespec="seconds"),)):
        out[r["zone"]][datetime.fromisoformat(r["h"] + ":00:00")] = r["l"]
    return out


# ───────────────────────── summaries ─────────────────────────

def daily_totals(conn, days: int = 14):
    ref = reference_time(conn)
    start = (ref - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute(
        "SELECT substr(ts,1,10) AS d, SUM(liters) AS l FROM readings WHERE ts >= ? GROUP BY d ORDER BY d",
        (start.isoformat(timespec="seconds"),),
    ).fetchall()
    have = {r["d"]: r["l"] for r in rows}
    return [
        {"date": (d := (start + timedelta(days=i)).date().isoformat()), "liters": round(have.get(d, 0.0), 1)}
        for i in range(days)
    ]


def hourly_today(conn):
    ref = reference_time(conn)
    day = ref.date().isoformat()
    rows = conn.execute(
        "SELECT CAST(substr(ts,12,2) AS INTEGER) AS h, SUM(liters) AS l FROM readings WHERE substr(ts,1,10)=? GROUP BY h",
        (day,),
    ).fetchall()
    have = {r["h"]: r["l"] for r in rows}
    return {"date": day, "hours": [{"hour": h, "liters": round(have.get(h, 0.0), 1)} for h in range(24)]}


def zone_breakdown(conn, days: int = 7):
    ref = reference_time(conn)
    start = (ref - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute(
        """SELECT z.id, z.name, z.icon, COALESCE(SUM(r.liters),0) AS l
           FROM zones z LEFT JOIN readings r ON r.zone_id = z.id AND r.ts >= ?
           GROUP BY z.id ORDER BY l DESC""",
        (start.isoformat(timespec="seconds"),),
    ).fetchall()
    total = sum(r["l"] for r in rows) or 1
    return [
        {"zone_id": r["id"], "zone": r["name"], "icon": r["icon"],
         "liters": round(r["l"], 1), "share": round(100 * r["l"] / total, 1)}
        for r in rows
    ]


def summary(conn):
    s = get_settings(conn)
    budget = s["household_size"] * s["lpcd_target"]
    days = daily_totals(conn, 14)
    today = days[-1]["liters"]
    prev = [d["liters"] for d in days[-8:-1] if d["liters"] > 0]
    avg7 = mean(prev) if prev else 0.0
    alerts = detect_anomalies(conn)
    wasted = sum(a.get("est_wasted_l_per_day", 0) for a in alerts if a["type"] == "leak")
    return {
        "today_liters": today,
        "avg_7d_liters": round(avg7, 1),
        "daily_budget_liters": budget,
        "budget_used_pct": round(100 * today / budget, 1) if budget else 0,
        "household_size": s["household_size"],
        "lpcd_target": s["lpcd_target"],
        "per_capita_today": round(today / s["household_size"], 1),
        "est_leak_loss_l_per_day": round(wasted, 1),
        "active_alerts": len(alerts),
        "as_of": reference_time(conn).isoformat(timespec="minutes"),
    }


# ───────────────────────── anomaly detection ─────────────────────────

def detect_anomalies(conn):
    ref = reference_time(conn)
    s = get_settings(conn)
    budget = s["household_size"] * s["lpcd_target"]
    alerts = []

    hist_start = ref - timedelta(days=30)
    hourly = _hourly(conn, hist_start)

    # 1) Night-flow leak: water flowing every hour between 01:00-04:59, on recent nights
    recent_days = [(ref - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0) for i in range(0, 4)]
    for zone, series in hourly.items():
        leaky_nights, night_avgs = [], []
        for day in recent_days:
            vals = [series.get(day + timedelta(hours=h), 0.0) for h in NIGHT_HOURS]
            if all(v >= MIN_NIGHT_FLOW_L for v in vals):
                leaky_nights.append(day.date().isoformat())
                night_avgs.append(mean(vals))
        if leaky_nights:
            rate = mean(night_avgs)
            alerts.append({
                "fingerprint": f"leak:{zone}",  # one alert per zone while the leak persists
                "type": "leak",
                "severity": "high" if len(leaky_nights) >= 2 else "medium",
                "zone": zone,
                "title": f"Possible leak in {zone}",
                "detail": (f"Water flowed every hour between 1–5 AM on {len(leaky_nights)} recent night(s) "
                           f"(≈{rate:.1f} L/hour while nobody is using it)."),
                "est_wasted_l_per_day": round(rate * 24, 1),
                "dates": leaky_nights,
            })

    # 2) Spike: hourly usage far above the same zone + same hour-of-day baseline
    cutoff = ref - timedelta(hours=48)
    for zone, series in hourly.items():
        by_hod = defaultdict(list)
        for t, v in series.items():
            if t < cutoff:
                by_hod[t.hour].append(v)
        spikes = []
        for t, v in sorted(series.items()):
            if t < cutoff or v < MIN_SPIKE_L:
                continue
            base = by_hod.get(t.hour, [])
            if len(base) < BASELINE_MIN_SAMPLES:
                continue
            mu, sd = mean(base), max(pstdev(base), 0.1 * mean(base) + 1)
            z = (v - mu) / sd
            if z >= SPIKE_SIGMA and v >= 1.8 * mu:
                spikes.append((t, v, mu, z))
        if spikes:
            extra = sum(v - mu for _, v, mu, _ in spikes)
            first, last = spikes[0][0], spikes[-1][0]
            alerts.append({
                "fingerprint": f"spike:{zone}:{first:%Y-%m-%dT%H}",
                "type": "spike",
                "severity": "high" if extra > 150 else "medium",
                "zone": zone,
                "title": f"Unusual usage in {zone}",
                "detail": (f"{len(spikes)} hour(s) between {first:%d %b %H:%M} and {last:%H:%M} were far above normal "
                           f"for that time of day (≈{extra:.0f} L more than usual)."),
                "est_wasted_l_per_day": 0,
                "extra_liters": round(extra, 1),
                "dates": [first.date().isoformat()],
            })

    # 3) Over daily budget (last 3 days)
    for d in daily_totals(conn, 14)[-3:]:
        if d["liters"] > budget * 1.1:
            over = d["liters"] - budget
            alerts.append({
                "fingerprint": f"budget:{d['date']}",
                "type": "budget",
                "severity": "medium",
                "zone": None,
                "title": f"Over daily budget on {datetime.fromisoformat(d['date']):%d %b}",
                "detail": f"Used {d['liters']:.0f} L against a budget of {budget:.0f} L (+{over:.0f} L).",
                "est_wasted_l_per_day": 0,
                "dates": [d["date"]],
            })

    alerts.extend(offline_devices(conn))

    order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: order[a["severity"]])
    return alerts


def offline_devices(conn, now: datetime | None = None):
    """Sensors that stopped reporting. Uses wall-clock time, not the data's reference time,
    because a silent sensor is exactly the case where no new data arrives."""
    now = now or datetime.now()
    limit = timedelta(minutes=config.device_offline_minutes)
    out = []
    rows = conn.execute(
        """SELECT d.id, d.name, d.last_seen, z.name AS zone FROM devices d
           JOIN zones z ON z.id = d.zone_id WHERE d.active = 1"""
    ).fetchall()
    for d in rows:
        if d["last_seen"] is None:
            continue  # registered but never installed: not an outage
        silent = now - datetime.fromisoformat(d["last_seen"])
        if silent > limit:
            mins = int(silent.total_seconds() // 60)
            out.append({
                "fingerprint": f"offline:{d['id']}:{d['last_seen']}",
                "type": "offline",
                "severity": "high" if silent > timedelta(hours=6) else "medium",
                "zone": d["zone"],
                "title": f"Sensor '{d['name']}' is offline",
                "detail": (f"No data from the {d['zone']} sensor for {mins // 60} h {mins % 60} min "
                           f"(last seen {d['last_seen']}). Leaks in this zone cannot be detected until it reconnects. "
                           "Check power and Wi-Fi."),
                "est_wasted_l_per_day": 0,
                "device_id": d["id"],
                "dates": [d["last_seen"][:10]],
            })
    return out


# ───────────────────────── recommendations ─────────────────────────

def recommendations(conn):
    recs = []
    alerts = detect_anomalies(conn)
    breakdown = zone_breakdown(conn, 7)
    share = {b["zone"]: b["share"] for b in breakdown}
    liters = {b["zone"]: b["liters"] for b in breakdown}

    for a in alerts:
        if a["type"] == "leak":
            recs.append({
                "priority": "high",
                "title": f"Inspect the {a['zone']} for leaks today",
                "action": "Check the cistern flush valve / float, taps and joints. Add a drop of food colour to the cistern and see if it shows in the bowl without flushing.",
                "saves_l_per_month": round(a["est_wasted_l_per_day"] * 30),
            })
        elif a["type"] == "spike":
            recs.append({
                "priority": "medium",
                "title": f"Avoid running water unattended in the {a['zone']}",
                "action": "Use a timer or auto shut-off nozzle so a hose or tap cannot stay open by mistake.",
                "saves_l_per_month": round(a.get("extra_liters", 0)),
            })
        elif a["type"] == "offline":
            recs.append({
                "priority": "high",
                "title": f"Reconnect the {a['zone']} flow sensor",
                "action": "Check the sensor's power supply and Wi-Fi. Until it reports again, leaks in this zone go unnoticed.",
                "saves_l_per_month": 0,
            })

    if share.get("Bathroom", 0) > 35:
        recs.append({
            "priority": "medium", "title": "Cut bathroom water use",
            "action": "Fit low-flow showerheads / tap aerators and keep showers under 5 minutes.",
            "saves_l_per_month": round(liters["Bathroom"] * 0.25 / 7 * 30),
        })
    if share.get("Garden", 0) > 15:
        recs.append({
            "priority": "medium", "title": "Make garden watering efficient",
            "action": "Switch to drip irrigation, water before 8 AM, and mulch the beds.",
            "saves_l_per_month": round(liters["Garden"] * 0.4 / 7 * 30),
        })
    if share.get("Laundry", 0) > 12:
        recs.append({
            "priority": "low", "title": "Run only full laundry loads",
            "action": "Wait for a full drum and reuse the rinse water for mopping or flushing.",
            "saves_l_per_month": round(liters["Laundry"] * 0.2 / 7 * 30),
        })
    if share.get("Kitchen", 0) > 15:
        recs.append({
            "priority": "low", "title": "Rinse vegetables in a bowl, not under a running tap",
            "action": "Reuse that water for plants.",
            "saves_l_per_month": round(liters["Kitchen"] * 0.15 / 7 * 30),
        })
    if not recs:
        recs.append({"priority": "low", "title": "Usage looks healthy",
                     "action": "Keep logging readings; JalRakshak will alert you the moment something looks off.",
                     "saves_l_per_month": 0})
    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: (order[r["priority"]], -r["saves_l_per_month"]))
    return recs
