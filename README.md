# JalRakshak 💧
Software-based smart water conservation & monitoring system.
Tracks consumption per zone, flags leaks / unusual usage / over-budget days, and recommends how to save water.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd backend
uvicorn main:app --reload
```
Open http://127.0.0.1:8000 (dashboard) or http://127.0.0.1:8000/docs (API docs).
Demo data (30 days, with a planted toilet leak + garden hose spike) is seeded on first start.

## Structure
```
backend/   FastAPI + SQLite   main.py (API) · analytics.py (detection + recommendations) · seed.py · database.py
frontend/  index.html · style.css · app.js (Chart.js dashboard)
```

## Detection logic (explainable, rule-based)
| Alert | Rule |
|---|---|
| Possible leak | Water flows every hour between 1–5 AM in a zone on recent nights |
| Unusual usage | Hourly use ≥ 3σ and ≥ 1.8× the zone's normal for that hour of day |
| Over budget | Daily total > 110% of household size × per-person target (default 135 L, BIS) |

## API
`POST /api/readings` · `GET /api/summary` · `/api/usage/daily|hourly|zones` · `/api/alerts` · `/api/recommendations` · `GET/PUT /api/settings` · `POST /api/demo/reseed`

## Future scope
Real IoT flow-sensor ingestion (ESP32 + YF-S201), ML forecasting, SMS/WhatsApp alerts, multi-household / campus mode.
