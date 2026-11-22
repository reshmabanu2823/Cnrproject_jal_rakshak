"""Forecasting, bill estimation and monthly reports.

Forecasts use the household's own hour-of-day profile (last 14 days), so they stay
explainable: "you usually use X L between now and midnight".
"""
import calendar
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean

import analytics
from database import get_settings


def bill(kl: float, slabs: list[dict], fixed_charge: float = 0.0) -> dict:
    """Apply a slab tariff. Each slab charges `rate` per kL for usage up to `upto_kl`
    (cumulative); the last slab has upto_kl = None (no upper limit)."""
    remaining, lower, energy, lines = max(kl, 0.0), 0.0, 0.0, []
    for s in slabs:
        upper = s["upto_kl"]
        width = remaining if upper is None else max(0.0, min(remaining, upper - lower))
        if width > 0:
            cost = width * s["rate"]
            energy += cost
            lines.append({"from_kl": lower, "to_kl": upper, "kl": round(width, 3),
                          "rate": s["rate"], "amount": round(cost, 2)})
            remaining -= width
        if upper is not None:
            lower = upper
        if remaining <= 1e-9:
            break
    return {"kl": round(kl, 3), "slabs": lines, "usage_charge": round(energy, 2),
            "fixed_charge": round(fixed_charge, 2), "total": round(energy + fixed_charge, 2)}


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), month % 12 + 1, 1)
    return start, end


def _total(conn, start: datetime, end: datetime) -> float:
    r = conn.execute("SELECT COALESCE(SUM(liters),0) AS l FROM readings WHERE ts >= ? AND ts < ?",
                     (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchone()
    return float(r["l"])


def hour_profile(conn, ref: datetime, days: int = 14) -> dict[int, float]:
    """Average litres per hour-of-day over the `days` full days before `ref`'s day."""
    day0 = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day0 - timedelta(days=days)
    rows = conn.execute(
        """SELECT substr(ts,1,10) AS d, CAST(substr(ts,12,2) AS INTEGER) AS h, SUM(liters) AS l
           FROM readings WHERE ts >= ? AND ts < ? GROUP BY d, h""",
        (start.isoformat(timespec="seconds"), day0.isoformat(timespec="seconds")),
    ).fetchall()
    active_days = {r["d"] for r in rows}
    n = len(active_days) or 1
    sums = defaultdict(float)
    for r in rows:
        sums[r["h"]] += r["l"]
    return {h: sums[h] / n for h in range(24)}


def forecast(conn: sqlite3.Connection) -> dict:
    s = get_settings(conn)
    ref = analytics.reference_time(conn)
    budget = s["household_size"] * s["lpcd_target"]
    profile = hour_profile(conn, ref)

    day0 = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    today_so_far = _total(conn, day0, ref.replace(minute=59, second=59) + timedelta(seconds=1))
    rest_of_day = sum(profile[h] for h in range(ref.hour + 1, 24))
    today_proj = today_so_far + rest_of_day

    days = analytics.daily_totals(conn, 8)
    prev7 = [d["liters"] for d in days[:-1] if d["liters"] > 0]
    avg_daily = mean(prev7) if prev7 else today_proj

    m_start, m_end = _month_bounds(ref.year, ref.month)
    mtd = _total(conn, m_start, day0) + today_proj
    days_in_month = calendar.monthrange(ref.year, ref.month)[1]
    days_left = days_in_month - ref.day
    month_proj = mtd + avg_daily * days_left

    # Week-over-week trend
    week = analytics.daily_totals(conn, 14)
    this_w = sum(d["liters"] for d in week[-7:])
    last_w = sum(d["liters"] for d in week[:7])
    trend = round(100 * (this_w - last_w) / last_w, 1) if last_w else None

    month_budget = budget * days_in_month
    return {
        "as_of": ref.isoformat(timespec="minutes"),
        "today": {
            "so_far_liters": round(today_so_far, 1),
            "projected_liters": round(today_proj, 1),
            "budget_liters": budget,
            "will_exceed_budget": today_proj > budget,
        },
        "month": {
            "month": f"{ref:%Y-%m}",
            "to_date_liters": round(mtd, 1),
            "projected_liters": round(month_proj, 1),
            "budget_liters": round(month_budget, 1),
            "projected_vs_budget_pct": round(100 * month_proj / month_budget, 1) if month_budget else None,
            "projected_bill": bill(month_proj / 1000, s["tariff_slabs"], s["fixed_charge"]),
            "currency": s["currency"],
        },
        "week_over_week_change_pct": trend,
        "method": "today = used so far + your average for the remaining hours (last 14 days); "
                  "month = month-to-date + 7-day daily average x days left.",
    }


def monthly_report(conn: sqlite3.Connection, month: str) -> dict:
    year, mon = (int(x) for x in month.split("-"))
    start, end = _month_bounds(year, mon)
    s = get_settings(conn)
    budget = s["household_size"] * s["lpcd_target"]
    q = (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))

    daily = conn.execute(
        "SELECT substr(ts,1,10) AS d, SUM(liters) AS l FROM readings WHERE ts >= ? AND ts < ? GROUP BY d ORDER BY d", q
    ).fetchall()
    zones = conn.execute(
        """SELECT z.name AS zone, z.icon, COALESCE(SUM(r.liters),0) AS l FROM zones z
           LEFT JOIN readings r ON r.zone_id = z.id AND r.ts >= ? AND r.ts < ?
           GROUP BY z.id ORDER BY l DESC""", q
    ).fetchall()
    alerts = conn.execute(
        "SELECT type, COUNT(*) AS n FROM alerts WHERE first_seen >= ? AND first_seen < ? GROUP BY type", q
    ).fetchall()

    total = sum(r["l"] for r in daily)
    n_days = len(daily)
    over = [r["d"] for r in daily if r["l"] > budget * 1.1]
    peak = max(daily, key=lambda r: r["l"]) if daily else None
    return {
        "month": f"{year:04d}-{mon:02d}",
        "total_liters": round(total, 1),
        "days_with_data": n_days,
        "avg_daily_liters": round(total / n_days, 1) if n_days else 0,
        "avg_lpcd": round(total / n_days / s["household_size"], 1) if n_days else 0,
        "lpcd_target": s["lpcd_target"],
        "days_over_budget": over,
        "peak_day": {"date": peak["d"], "liters": round(peak["l"], 1)} if peak else None,
        "zones": [{"zone": z["zone"], "icon": z["icon"], "liters": round(z["l"], 1),
                   "share": round(100 * z["l"] / total, 1) if total else 0} for z in zones],
        "alerts_by_type": {a["type"]: a["n"] for a in alerts},
        "bill": bill(total / 1000, s["tariff_slabs"], s["fixed_charge"]),
        "currency": s["currency"],
    }


def valid_month(m: str) -> bool:
    try:
        date.fromisoformat(m + "-01")
        return len(m) == 7
    except ValueError:
        return False
