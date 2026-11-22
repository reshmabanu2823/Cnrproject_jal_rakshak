import os
import sys
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="jalrakshak-test-")
os.environ["JALRAKSHAK_DB"] = str(Path(_tmp) / "test.db")
os.environ["JALRAKSHAK_ALERT_SCAN_SECONDS"] = "0"
os.environ["JALRAKSHAK_DEMO_MODE"] = "true"
os.environ["JALRAKSHAK_WEBHOOK_MAX_ATTEMPTS"] = "1"
os.environ.pop("JALRAKSHAK_ADMIN_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from config import config  # noqa: E402
from database import connect  # noqa: E402
from security import limiter  # noqa: E402
from seed import seed  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_state(client, monkeypatch):
    """Every test starts from the seeded demo dataset with no devices, alerts or webhooks."""
    with connect() as conn:
        for t in ("notification_log", "alerts", "webhooks", "audit_log"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM readings")
        conn.execute("DELETE FROM devices")
        conn.execute("DELETE FROM zones WHERE id > 6")
        conn.execute("UPDATE settings SET value='4' WHERE key='household_size'")
    seed()
    limiter.reset()
    monkeypatch.setattr(config, "admin_api_key", None)
    yield


@pytest.fixture
def admin_key(monkeypatch):
    monkeypatch.setattr(config, "admin_api_key", "s3cret-admin-key")
    return {"X-API-Key": "s3cret-admin-key"}
