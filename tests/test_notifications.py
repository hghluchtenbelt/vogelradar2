"""The push loop: one bad registration must never cost the others their alerts."""
from __future__ import annotations

import json

import pytest

import notifications

GOOD = "good-token-" + "a" * 40
OTHER = "other-token-" + "b" * 40
BAD = "bad-token-" + "c" * 40

SIGHTING = {
    "bird_name": "Kuifmees", "rarity": 3, "latitude": 52.10, "longitude": 5.10,
    "location": "Utrecht - Griftpark", "date": "2026-11-14", "obs_time": "09:12",
    "url": "https://waarneming.nl/observation/1/",
}


class FakeMessaging:
    """Stands in for firebase_admin.messaging; records instead of sending."""

    class Message:
        def __init__(self, data, token):
            self.data, self.token = data, token

    def __init__(self):
        self.sent: list = []
        self.failures: dict[str, Exception] = {}

    def send(self, msg):
        if msg.token in self.failures:
            raise self.failures[msg.token]
        self.sent.append(msg)

    def tokens(self) -> list[str]:
        return [m.token for m in self.sent]


@pytest.fixture
def fcm(db, tmp_path, monkeypatch):
    cred = tmp_path / "firebase.json"
    cred.write_text("{}")
    fake = FakeMessaging()
    monkeypatch.setattr(notifications, "_SERVICE_ACCOUNT", cred)
    monkeypatch.setattr(notifications, "_messaging", lambda: fake)
    return fake


def _raw_row(db, token, lat, lng, wishlist='["Kuifmees"]', max_dist=50):
    """Insert a row the way a pre-validation server would have stored it."""
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO push_subscribers VALUES (?, ?, ?, ?, ?, ?)",
            (token, lat, lng, wishlist, max_dist, "2026-11-14T00:00:00"),
        )
        conn.commit()


def _tokens(db) -> set[str]:
    with db._connect() as conn:
        return {r[0] for r in conn.execute("SELECT fcm_token FROM push_subscribers")}


def test_matching_subscriber_gets_a_push(db, fcm):
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    notifications.send_push_notifications([SIGHTING])
    assert fcm.tokens() == [GOOD]
    data = fcm.sent[0].data
    # Keys the released app reads; all values must be strings for FCM.
    assert set(data) == {"title", "body", "bird_name", "url", "lat", "lng"}
    assert all(isinstance(v, str) for v in data.values())
    assert data["title"] == "Kuifmees gespot!"


@pytest.mark.parametrize("lat, lng", [
    (None, 5.1),        # how SQLite stores a NaN
    (52.1, None),
    (float("inf"), 5.1),
    (123.0, 5.1),
    ("abc", 5.1),
])
def test_bad_coordinates_do_not_block_later_subscribers(db, fcm, lat, lng):
    _raw_row(db, BAD, lat, lng)
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.upsert_push_subscriber(OTHER, 52.11, 5.09, '["__rare__"]', 50)

    notifications.send_push_notifications([SIGHTING])

    assert fcm.tokens() == [GOOD, OTHER]
    assert _tokens(db) == {GOOD, OTHER}      # the unusable row is gone


@pytest.mark.parametrize("wishlist", ["not json", '{"a": 1}', '"Kuifmees"', "null"])
def test_malformed_wishlist_does_not_block_later_subscribers(db, fcm, wishlist):
    _raw_row(db, BAD, 52.09, 5.12, wishlist=wishlist)
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)

    notifications.send_push_notifications([SIGHTING])

    assert fcm.tokens() == [GOOD]
    assert _tokens(db) == {GOOD}


def test_failure_while_serving_one_subscriber_does_not_stop_the_rest(db, fcm, monkeypatch):
    db.upsert_push_subscriber(BAD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    real = notifications.already_notified

    def flaky(token, *args, **kwargs):
        if token == BAD:
            raise RuntimeError("database is locked")
        return real(token, *args, **kwargs)

    monkeypatch.setattr(notifications, "already_notified", flaky)
    notifications.send_push_notifications([SIGHTING])

    assert fcm.tokens() == [GOOD]
    assert _tokens(db) == {BAD, GOOD}        # a transient error keeps the row


def test_transient_send_error_keeps_the_subscriber(db, fcm):
    db.upsert_push_subscriber(BAD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    fcm.failures[BAD] = RuntimeError("503 backend unavailable")

    notifications.send_push_notifications([SIGHTING])

    assert fcm.tokens() == [GOOD]
    assert _tokens(db) == {BAD, GOOD}


class UnregisteredError(Exception):
    """Same class name as firebase_admin.messaging.UnregisteredError."""


@pytest.mark.parametrize("error", [
    UnregisteredError("gone"),
    Exception("Requested entity was not found."),
    Exception("The registration token is not a valid FCM registration token"),
])
def test_dead_token_is_removed(db, fcm, error):
    db.upsert_push_subscriber(BAD, 52.09, 5.12, '["Kuifmees"]', 50)
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    fcm.failures[BAD] = error

    notifications.send_push_notifications([SIGHTING])

    assert fcm.tokens() == [GOOD]
    assert _tokens(db) == {GOOD}


def test_payload_error_does_not_delete_subscribers(db, fcm):
    # An invalid-argument error about the message (our bug) is not a dead token.
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    fcm.failures[GOOD] = Exception("Invalid value at 'message.data' (TYPE_STRING)")
    notifications.send_push_notifications([SIGHTING])
    assert _tokens(db) == {GOOD}


def test_sighting_without_coordinates_is_skipped_not_fatal(db, fcm):
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    broken = {**SIGHTING, "latitude": None}
    notifications.send_push_notifications([broken, SIGHTING])
    assert fcm.tokens() == [GOOD]


def test_distance_and_wishlist_are_respected(db, fcm):
    db.upsert_push_subscriber(GOOD, 53.2, 6.5, '["Kuifmees"]', 25)        # Groningen, too far
    db.upsert_push_subscriber(OTHER, 52.09, 5.12, '["Kwak"]', 50)          # other species
    notifications.send_push_notifications([SIGHTING])
    assert fcm.sent == []


def test_without_credentials_nothing_is_sent(db, monkeypatch, tmp_path):
    monkeypatch.setattr(notifications, "_SERVICE_ACCOUNT", tmp_path / "absent.json")
    monkeypatch.setattr(notifications, "_messaging",
                        lambda: pytest.fail("must not initialise firebase"))
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Kuifmees"]', 50)
    notifications.send_push_notifications([SIGHTING])


def test_prune_removes_unusable_rows_and_their_history(db):
    _raw_row(db, BAD, None, 5.1)
    _raw_row(db, "empty-" + "d" * 40, 52.0, 5.0, wishlist="[]")
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, json.dumps(["Kuifmees"]), 50)
    db.record_notification(BAD, "Kuifmees", 52.1, 5.1)
    db.record_notification(GOOD, "Kuifmees", 52.1, 5.1)

    assert db.prune_subscribers() == 2
    assert _tokens(db) == {GOOD}
    with db._connect() as conn:
        left = {r[0] for r in conn.execute("SELECT token FROM sent_notifications")}
    assert left == {GOOD}
