"""Push dedupe across scrape runs, and pushes that survive a restart."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from test_notifications import GOOD, fcm  # noqa: F401 (fixture)

import notifications
import updater


def _sighting(n: int, name: str = "Kuifmees", lat: float = 52.10, lng: float = 5.10) -> dict:
    return {
        "url": f"https://waarneming.nl/observation/{n}/", "bird_name": name,
        "location": "Utrecht - Griftpark", "date": "2026-11-14", "obs_time": "09:12",
        "count": 1, "photo": 0, "latitude": lat, "longitude": lng, "rarity": 3,
    }


def _ago(hours: float) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat(timespec="seconds")


def _age_rows(db, table: str, column: str, hours: float) -> None:
    with db._connect() as conn:
        conn.execute(f"UPDATE {table} SET {column} = ?", (_ago(hours),))
        conn.commit()


def _pending(db) -> set[str]:
    with db._connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT url FROM sightings WHERE push_pending = 1")}


@pytest.fixture
def scrape(db, monkeypatch):
    """Run updater.run_update() with a scripted scraper instead of the network."""
    monkeypatch.setattr(updater, "run_revalidation", lambda *a, **k: (0, 0, 0))

    def run(batch: list[dict], crash: bool = False):
        def fake_fetch(*, known_urls, on_observations, **_):
            on_observations([s for s in batch if s["url"] not in known_urls])
            if crash:
                raise KeyboardInterrupt("container stopped for a deploy")
            return []

        monkeypatch.setattr(updater, "fetch_rare_birds", fake_fetch)
        return updater.run_update()

    return run


# ── dedupe window ────────────────────────────────────────────────


def test_dedupe_window_is_longer_than_the_scrape_interval(db):
    assert db.NOTIFY_DEDUPE_HOURS * 60 > 2 * 60     # scrapes run at least hourly


@pytest.mark.parametrize("hours_ago, expected", [(1.5, True), (23, True), (25, False)])
def test_already_notified_looks_back_the_whole_window(db, hours_ago, expected):
    db.record_notification(GOOD, "Kuifmees", 52.10, 5.10)
    _age_rows(db, "sent_notifications", "sent_at", hours_ago)
    assert db.already_notified(GOOD, "Kuifmees", 52.11, 5.11) is expected


def test_recording_keeps_history_for_the_whole_window(db):
    # The old code pruned at 2 hours, which made any longer window useless.
    db.record_notification(GOOD, "Kuifmees", 52.10, 5.10)
    _age_rows(db, "sent_notifications", "sent_at", 5)
    db.record_notification(GOOD, "Kwak", 51.0, 4.0)
    assert db.already_notified(GOOD, "Kuifmees", 52.10, 5.10)

    _age_rows(db, "sent_notifications", "sent_at", 30)
    db.record_notification(GOOD, "Kwak", 51.0, 4.0)
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sent_notifications").fetchone()[0] == 1


def test_staked_out_bird_is_announced_once_across_runs(db, fcm, scrape):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.insert_sightings([_sighting(0)])               # database is not empty

    scrape([_sighting(1)])
    assert len(fcm.sent) == 1

    # Next hourly run: another observer reports the same bird 300 m away.
    _age_rows(db, "sent_notifications", "sent_at", 1.2)
    scrape([_sighting(2, lat=52.103, lng=5.102)])
    assert len(fcm.sent) == 1

    # A different species at the same place still gets through,
    # and so does the same species elsewhere.
    scrape([_sighting(3, name="Kwak"), _sighting(4, lat=52.30, lng=5.40)])
    assert [m.data["bird_name"] for m in fcm.sent] == ["Kuifmees", "Kuifmees"]

    # The next day it is worth saying again.
    _age_rows(db, "sent_notifications", "sent_at", 25)
    scrape([_sighting(5)])
    assert len(fcm.sent) == 3


# ── pushes survive a restart ─────────────────────────────────────


def test_new_sightings_are_announced_and_cleared(db, fcm, scrape):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.insert_sightings([_sighting(0)])
    assert scrape([_sighting(1)]) == (1, 1)
    assert [m.data["url"] for m in fcm.sent] == ["https://waarneming.nl/observation/1/"]
    assert _pending(db) == set()


def test_interrupted_scrape_is_announced_by_the_next_run(db, fcm, scrape):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.insert_sightings([_sighting(0)])

    with pytest.raises(KeyboardInterrupt):
        scrape([_sighting(1)], crash=True)
    assert fcm.sent == []                                   # never got to the push loop
    assert _pending(db) == {"https://waarneming.nl/observation/1/"}

    scrape([])                                              # nothing new on the site
    assert [m.data["url"] for m in fcm.sent] == ["https://waarneming.nl/observation/1/"]
    assert _pending(db) == set()


def test_pending_pushes_can_be_flushed_at_startup(db, fcm):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.insert_sightings([_sighting(1)], push_pending=True)
    assert updater.send_pending_pushes() == 1
    assert len(fcm.sent) == 1
    assert updater.send_pending_pushes() == 0
    assert len(fcm.sent) == 1


def test_first_run_on_an_empty_database_announces_nothing(db, fcm, scrape):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    assert scrape([_sighting(1), _sighting(2)]) == (2, 2)
    assert fcm.sent == []
    assert _pending(db) == set()


def test_stale_pending_pushes_are_dropped_not_sent(db, fcm, scrape):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.insert_sightings([_sighting(1)], push_pending=True)
    _age_rows(db, "sightings", "scraped_at", updater.PUSH_MAX_AGE_H + 1)
    scrape([])
    assert fcm.sent == []
    assert _pending(db) == set()


def test_pending_is_cleared_when_push_is_not_configured(db, scrape, monkeypatch, tmp_path):
    # Staging and local runs have no Firebase credentials.
    monkeypatch.setattr(notifications, "_SERVICE_ACCOUNT", tmp_path / "absent.json")
    db.insert_sightings([_sighting(0)])
    scrape([_sighting(1)])
    assert _pending(db) == set()


def test_failed_push_run_is_retried(db, fcm, scrape, monkeypatch):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.insert_sightings([_sighting(0)])
    real = notifications.send_push_notifications

    def down(_):
        raise RuntimeError("FCM unreachable")

    monkeypatch.setattr(notifications, "send_push_notifications", down)
    scrape([_sighting(1)])
    assert _pending(db) == {"https://waarneming.nl/observation/1/"}

    monkeypatch.setattr(notifications, "send_push_notifications", real)
    scrape([])
    assert len(fcm.sent) == 1
    assert _pending(db) == set()


def test_adding_the_column_marks_existing_rows_as_handled(db, fcm, tmp_path, monkeypatch):  # noqa: F811
    """A production database from before this change must not flood."""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute(
        """CREATE TABLE sightings (
            url TEXT PRIMARY KEY, bird_name TEXT NOT NULL, location TEXT, date TEXT,
            obs_time TEXT DEFAULT '', count INTEGER DEFAULT 1, photo INTEGER DEFAULT 0,
            photo_url TEXT DEFAULT '', latitude REAL NOT NULL, longitude REAL NOT NULL,
            scraped_at TEXT NOT NULL, rarity INTEGER DEFAULT 3)"""
    )
    conn.executemany(
        "INSERT INTO sightings (url, bird_name, latitude, longitude, scraped_at)"
        " VALUES (?, 'Kuifmees', 52.1, 5.1, ?)",
        [(f"https://waarneming.nl/observation/{i}/", _ago(0.5)) for i in range(50)],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", old)
    db.init_db()
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)

    assert db.get_pending_pushes(updater.PUSH_MAX_AGE_H) == []
    assert updater.send_pending_pushes() == 0
    assert fcm.sent == []
