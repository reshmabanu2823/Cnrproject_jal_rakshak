# JalRakshak 💧
Software-based smart water conservation & monitoring system.
Tracks consumption per zone, flags leaks / unusual usage / over-budget days / offline sensors,
forecasts this month's usage and water bill, and recommends how to save water.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd backend
uvicorn main:app --reload
```
Open http://127.0.0.1:8000 (dashboard) or http://127.0.0.1:8000/docs (interactive API docs).
Demo data (30 days, with a planted toilet leak + garden hose spike) is seeded on first start.

### Tests
```bash
pip install -r requirements-dev.txt
cd backend && python -m pytest -q
```

### Production (Docker)
```bash
docker build -t jalrakshak .
docker run -d -p 8000:8000 -v jalrakshak-data:/data \
  -e JALRAKSHAK_ADMIN_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" jalrakshak
```
The image runs with demo mode off, as a non-root user, with a health check. All settings are
environment variables; see [.env.example](.env.example). Put it behind HTTPS (Caddy / nginx) before
exposing it to the internet, because API keys travel in headers.

## Structure
```
backend/
  main.py        API routes, middleware, background alert scanner
  config.py      environment-based configuration
  database.py    SQLite connections, versioned migrations, settings, audit log
  security.py    admin key + per-device keys, rate limiting
  schemas.py     request validation
  analytics.py   detection (leak / spike / budget / offline) + recommendations
  alerts.py      alert lifecycle (open → acknowledged → resolved) + signed webhooks
  forecast.py    usage forecast, slab-tariff bill estimate, monthly report
  seed.py        demo data
  tests/         pytest suite
frontend/        index.html · style.css · app.js (Chart.js dashboard)
firmware/        ESP32 + YF-S201 flow-sensor sketch
tools/           simulate_device.py (fake sensor for demos)
```

## Detection logic (explainable, rule-based)
| Alert | Rule |
|---|---|
| Possible leak | Water flows every hour between 1–5 AM in a zone on recent nights |
| Unusual usage | Hourly use ≥ 3σ and ≥ 1.8× the zone's normal for that hour of day |
| Over budget | Daily total > 110% of household size × per-person target (default 135 L, BIS) |
| Sensor offline | A registered sensor has not reported for 30 min (configurable) |

Alerts are stored with a stable fingerprint, so one leak is one alert, not a new alert every scan.
A background task rescans every 60 s. Alerts whose condition clears are auto-resolved. An auto-resolved
alert re-opens if the condition returns. An alert a person resolved re-opens only on newer evidence
(for example, the leak shows up again on a later night).

## Security
* **Admin key**: set `JALRAKSHAK_ADMIN_KEY`. Every write/admin endpoint then needs
  `X-API-Key: <key>` or `Authorization: Bearer <key>`. Read endpoints stay public for the dashboard.
  Without the key the server runs open and logs a warning. That is for local dev only.
  For the dashboard, run `localStorage.setItem("jr_api_key", "<key>")` once in the browser console.
* **Device keys**: each sensor gets its own key (`jr_…`). The server stores only its SHA-256 hash.
  You can rotate or disable one sensor without touching the others.
* Rate limiting per IP and per device, request IDs (`X-Request-ID`), security headers,
  an audit log of admin actions, and parameterised SQL throughout.

## Connecting a real sensor
1. Register it: `POST /api/devices {"name": "Toilet line", "zone_id": 5}`. Copy `device_key`, which is shown once.
2. Flash [firmware/esp32_flow_sensor](firmware/esp32_flow_sensor/esp32_flow_sensor.ino) with your Wi-Fi, server URL and key.
   No hardware? Run `python tools/simulate_device.py --key jr_… --leak`.
3. The device POSTs batches to `/api/ingest` with `X-Device-Key`:
   ```json
   {"firmware": "1.0.0", "readings": [{"seq": 4294967297, "ts": "2026-11-22T06:00:00Z", "pulses": 900}]}
   ```
   Send `pulses` (converted with the device's `pulses_per_liter`, default 450 for YF-S201) or `liters`.
   A `seq` makes retries safe: duplicates are counted, not stored twice. Future timestamps are rejected.
   Readings with 0 L act as a heartbeat.

## Notifications
`POST /api/webhooks {"url": "https://…", "secret": "…", "min_severity": "high"}`. New, re-opened and
escalated alerts are POSTed as JSON. The body is signed in `X-JalRakshak-Signature: sha256=<HMAC>`.
Failed deliveries are retried with backoff and logged in `GET /api/notifications`.
Point the webhook at an SMS/WhatsApp gateway, Slack, Zapier or n8n. `POST /api/webhooks/{id}/test` sends a test.

## Billing & forecast
`GET /api/forecast` projects today's and this month's usage from your own hour-of-day pattern.
It also estimates the bill under a slab tariff. The default tariff is an **example**: set your
utility's slabs with `PUT /api/settings`:
```json
{"tariff_slabs": [{"upto_kl": 8, "rate": 7}, {"upto_kl": 25, "rate": 11}, {"upto_kl": null, "rate": 45}],
 "fixed_charge": 50, "currency": "INR"}
```

## API
| Area | Endpoints |
|---|---|
| Health | `GET /healthz` · `GET /readyz` |
| Readings | `POST /api/readings` · `GET /api/readings?zone_id&start&end&source&limit&offset` · `DELETE /api/readings/{id}` · `GET /api/readings/export.csv` · `POST /api/readings/import` (CSV body `ts,zone,liters`) |
| Zones | `GET/POST /api/zones` |
| Devices | `POST/GET /api/devices` · `PATCH /api/devices/{id}` · `POST /api/devices/{id}/rotate-key` · `POST /api/ingest` |
| Analytics | `GET /api/summary` · `/api/usage/daily\|hourly\|zones` · `/api/recommendations` · `/api/forecast` · `/api/bill/estimate?liters=` · `/api/reports/monthly?month=YYYY-MM` |
| Alerts | `GET /api/alerts?status=active\|open\|acknowledged\|resolved\|all` · `POST /api/alerts/{id}/acknowledge\|resolve\|reopen` |
| Notifications | `GET/POST /api/webhooks` · `DELETE /api/webhooks/{id}` · `POST /api/webhooks/{id}/test` · `GET /api/notifications` |
| Admin | `GET/PUT /api/settings` (partial updates) · `GET /api/audit` · `POST /api/demo/reseed` (demo mode only) |

## Limits / future scope
* Single household, single process. The rate limiter and alert loop live in memory. For several
  workers, move them to Redis and the DB to PostgreSQL.
* Next steps: multi-household / campus mode with user accounts, ML forecasting, tank-level sensors.
