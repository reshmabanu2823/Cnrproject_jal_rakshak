"""Generates realistic demo data: 30 days of hourly readings per zone.

Two problems are planted so the analytics have something to find:
  * a slow overnight leak in the Toilet cistern for the last 3 nights
  * a one-off Garden spike (hose left running) on the day before the latest reading
"""
import math
import random
from datetime import datetime, timedelta

from database import connect

# hourly litres (typical household, 4 people) by hour-of-day: (base weights per zone)
PROFILE = {
    "Kitchen":       {6: 8, 7: 14, 8: 10, 12: 12, 13: 10, 19: 18, 20: 16, 21: 8},
    "Bathroom":      {5: 20, 6: 70, 7: 85, 8: 45, 9: 15, 20: 30, 21: 40, 22: 20},
    "Laundry":       {9: 45, 10: 55, 11: 20},   # mostly weekends handled below
    "Garden":        {6: 35, 7: 25, 17: 20, 18: 25},
    "Toilet":        {6: 12, 7: 14, 8: 8, 12: 6, 15: 6, 19: 8, 21: 8, 22: 6},
    "Cleaning":      {8: 6, 11: 10, 16: 8, 18: 5},
}


def seed(days: int = 30, reset: bool = True) -> int:
    random.seed(42)
    with connect() as conn:
        if reset:
            conn.execute("DELETE FROM readings")
        # seeded zones are looked up by name; skip any the admin removed or renamed
        zones = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM zones")}
        end = datetime.now().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        rows = []
        t = start
        while t <= end:
            for name, prof in PROFILE.items():
                if name not in zones:
                    continue
                base = prof.get(t.hour, 0)
                weekend = t.weekday() >= 5
                if name == "Laundry" and not weekend:
                    base *= 0.35
                if weekend and name in ("Bathroom", "Kitchen"):
                    base *= 1.15
                litres = max(0.0, random.gauss(base * 0.72, base * 0.13)) if base else 0.0
                days_ago = (end.date() - t.date()).days

                # planted problem 1: overnight toilet leak, last 3 nights (00:00-05:00)
                if name == "Toilet" and days_ago <= 3 and t.hour in (0, 1, 2, 3, 4, 5):
                    litres += random.uniform(3.5, 5.0)

                # planted problem 2: garden hose left running yesterday evening
                if name == "Garden" and days_ago == 1 and t.hour in (17, 18, 19, 20):
                    litres += random.uniform(90, 130)

                if litres > 0.05:
                    rows.append((zones[name], t.isoformat(timespec="seconds"), round(litres, 2), "demo"))
            t += timedelta(hours=1)
        conn.executemany("INSERT INTO readings(zone_id, ts, liters, source) VALUES (?,?,?,?)", rows)
        return len(rows)


if __name__ == "__main__":
    from database import init_db
    init_db()
    print(f"seeded {seed()} readings")
