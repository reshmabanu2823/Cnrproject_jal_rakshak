"""Alert lifecycle and notifications.

Detection (analytics.detect_anomalies) is stateless: it recomputes from readings.
This module gives alerts a lifecycle so they behave like a real monitoring tool:

    open -> acknowledged -> resolved
      ^                        |
      +---- re-opened if the problem comes back after it was resolved

* One fingerprint = one alert row, so the same leak does not notify every scan.
* Conditions that clear on their own are auto-resolved.
* New / re-opened alerts are pushed to registered webhooks (signed with HMAC-SHA256),
  which can forward to SMS / WhatsApp / email gateways.
"""
import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import analytics
from config import config

log = logging.getLogger("jalrakshak.alerts")

_sync_lock = threading.Lock()
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
_PAYLOAD_KEYS = ("fingerprint", "type", "severity", "zone", "title", "detail")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def row_to_alert(r: sqlite3.Row) -> dict:
    a = dict(r)
    data = json.loads(a.pop("data") or "{}")
    return {**data, **a}


def sync(conn: sqlite3.Connection) -> list[dict]:
    """Reconcile detected conditions with stored alerts. Returns alerts that are new, re-opened or escalated.

    Serialised by a process lock plus BEGIN IMMEDIATE (for multiple workers), so two
    concurrent scans can never insert the same fingerprint twice."""
    with _sync_lock:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = _sync(conn)
            conn.commit()
            return fresh
        except BaseException:
            conn.rollback()
            raise


def _sync(conn: sqlite3.Connection) -> list[dict]:
    detected = analytics.detect_anomalies(conn)
    now = _now()
    fresh: list[dict] = []
    seen = set()
    for a in detected:
        fp = a["fingerprint"]
        seen.add(fp)
        data = json.dumps({k: v for k, v in a.items() if k not in _PAYLOAD_KEYS})
        row = conn.execute("SELECT * FROM alerts WHERE fingerprint = ?", (fp,)).fetchone()
        if row is None:
            cur = conn.execute(
                """INSERT INTO alerts(fingerprint, type, severity, zone, title, detail, data, status, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?, 'open', ?, ?)""",
                (fp, a["type"], a["severity"], a["zone"], a["title"], a["detail"], data, now, now),
            )
            fresh.append({"id": cur.lastrowid, "event": "alert.opened", **a})
            continue

        reopen = False
        if row["status"] == "resolved":
            # Auto-resolved: the condition is back, so re-open. Resolved by a person: re-open only if
            # the evidence is newer than their resolution (e.g. the leak shows up on a later night).
            newest = max(a.get("dates") or [""])
            reopen = row["resolved_by"] == "auto" or (bool(row["resolved_at"]) and newest > row["resolved_at"][:10])
        if reopen:
            conn.execute(
                """UPDATE alerts SET status='open', severity=?, title=?, detail=?, data=?, last_seen=?,
                   acknowledged_at=NULL, resolved_at=NULL, resolved_by=NULL WHERE id=?""",
                (a["severity"], a["title"], a["detail"], data, now, row["id"]),
            )
            fresh.append({"id": row["id"], "event": "alert.reopened", **a})
        elif row["status"] != "resolved":
            escalated = SEVERITY_RANK[a["severity"]] > SEVERITY_RANK[row["severity"]]
            conn.execute("UPDATE alerts SET severity=?, title=?, detail=?, data=?, last_seen=? WHERE id=?",
                         (a["severity"], a["title"], a["detail"], data, now, row["id"]))
            if escalated:
                fresh.append({"id": row["id"], "event": "alert.escalated", **a})

    # Auto-resolve anything that is no longer detected.
    for r in conn.execute("SELECT id, fingerprint FROM alerts WHERE status != 'resolved'").fetchall():
        if r["fingerprint"] not in seen:
            conn.execute("UPDATE alerts SET status='resolved', resolved_at=?, resolved_by='auto' WHERE id=?",
                         (now, r["id"]))
    return fresh


def list_alerts(conn: sqlite3.Connection, status: str | None = None, limit: int = 100) -> list[dict]:
    if status:
        rows = conn.execute("SELECT * FROM alerts WHERE status=? ORDER BY last_seen DESC LIMIT ?", (status, limit))
    else:
        rows = conn.execute("SELECT * FROM alerts ORDER BY last_seen DESC LIMIT ?", (limit,))
    out = [row_to_alert(r) for r in rows]
    out.sort(key=lambda a: (a["status"] == "resolved", -SEVERITY_RANK[a["severity"]]))
    return out


def transition(conn: sqlite3.Connection, alert_id: int, status: str, note: str | None, actor: str) -> dict | None:
    row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if row is None:
        return None
    now = _now()
    if status == "acknowledged":
        conn.execute("UPDATE alerts SET status='acknowledged', acknowledged_at=?, note=COALESCE(?, note) WHERE id=?",
                     (now, note, alert_id))
    elif status == "resolved":
        conn.execute("UPDATE alerts SET status='resolved', resolved_at=?, resolved_by=?, note=COALESCE(?, note) WHERE id=?",
                     (now, actor, note, alert_id))
    else:  # reopen
        conn.execute("UPDATE alerts SET status='open', acknowledged_at=NULL, resolved_at=NULL, resolved_by=NULL WHERE id=?",
                     (alert_id,))
    return row_to_alert(conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone())


# ───────────────────────── webhooks ─────────────────────────

def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, secret: str | None) -> int:
    headers = {"Content-Type": "application/json", "User-Agent": "JalRakshak-Webhook/1.0"}
    if secret:
        headers["X-JalRakshak-Signature"] = sign(secret, body)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=config.webhook_timeout_s) as resp:  # noqa: S310 (scheme validated on create)
        return resp.status


def deliver(conn: sqlite3.Connection, webhook: sqlite3.Row, event: dict) -> tuple[int | None, str | None]:
    """POST one event with retries + exponential backoff. Logs the outcome."""
    payload = {
        "event": event["event"],
        "sent_at": _now(),
        "alert": {k: event.get(k) for k in ("id", *_PAYLOAD_KEYS, "est_wasted_l_per_day")},
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    status, error, attempts = None, None, 0
    for attempt in range(1, max(1, config.webhook_max_attempts) + 1):
        attempts = attempt
        try:
            status = _post(webhook["url"], body, webhook["secret"])
            error = None
            if 200 <= status < 300:
                break
            error = f"HTTP {status}"
        except urllib.error.HTTPError as e:
            status, error = e.code, f"HTTP {e.code}"
            if 400 <= e.code < 500 and e.code != 429:
                break  # client error: retrying will not help
        except Exception as e:  # network errors, timeouts
            status, error = None, f"{type(e).__name__}: {e}"
        if attempt < config.webhook_max_attempts:
            time.sleep(min(2 ** (attempt - 1), 8))
    conn.execute("UPDATE webhooks SET last_status=?, last_error=? WHERE id=?", (status, error, webhook["id"]))
    conn.execute(
        "INSERT INTO notification_log(webhook_id, alert_id, event, ts, attempts, status_code, error) VALUES (?,?,?,?,?,?,?)",
        (webhook["id"], event.get("id"), event["event"], _now(), attempts, status, error),
    )
    conn.commit()  # never hold the write lock across the next HTTP call
    if error:
        log.warning("webhook %s failed for alert %s: %s", webhook["id"], event.get("id"), error)
    return status, error


def notify(conn: sqlite3.Connection, events: list[dict]) -> int:
    if not events:
        return 0
    hooks = conn.execute("SELECT * FROM webhooks WHERE active = 1").fetchall()
    sent = 0
    for ev in events:
        for h in hooks:
            if SEVERITY_RANK[ev["severity"]] >= SEVERITY_RANK.get(h["min_severity"], 1):
                deliver(conn, h, ev)
                sent += 1
    return sent
