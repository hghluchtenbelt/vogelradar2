"""Shared test setup.

The environment is prepared before any project module is imported, so the
tests can never touch the real database, start the background scraper, or
find Firebase credentials. Nothing in this suite uses the network.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="vogelradar-test-"))
os.environ["VOGELRADAR_DB_PATH"] = str(_TMP / "import-time.db")
os.environ["SCRAPE_INTERVAL_MIN"] = "0"
os.environ["FIREBASE_CREDENTIALS"] = str(_TMP / "absent.json")
os.environ["FORECAST_ENABLED"] = "0"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if a test would open a scraper session."""
    import scraper

    def _blocked(*args, **kwargs):
        raise AssertionError("tests must not contact waarneming.nl")

    monkeypatch.setattr(scraper, "_make_authenticated_session", _blocked)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, empty database per test."""
    import database

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    return database


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    import api

    api._rate_limit.clear()
    return TestClient(api.app)


# A token shaped like a real FCM registration token (length, charset).
TOKEN = "fGx7Qk2pT9u:APA91b" + "A1b2C3d4E5f6G7h8" * 8


def subscriber(token: str = TOKEN, **overrides) -> dict:
    body = {"token": token, "lat": 52.09, "lng": 5.12,
            "wishlist": ["Kuifmees"], "max_dist": 50}
    body.update(overrides)
    return body
