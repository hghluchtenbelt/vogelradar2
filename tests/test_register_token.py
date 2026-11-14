"""POST /register-token: only well-formed registrations are stored."""
from __future__ import annotations

import json

import pytest
from conftest import TOKEN, subscriber


def _stored(db) -> list[dict]:
    with db._connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM push_subscribers")]


def test_valid_registration_is_stored(client, db):
    r = client.post("/register-token", json=subscriber())
    assert r.status_code == 200
    rows = _stored(db)
    assert len(rows) == 1
    assert rows[0]["fcm_token"] == TOKEN
    assert rows[0]["lat"] == 52.09
    assert json.loads(rows[0]["wishlist"]) == ["Kuifmees"]


def test_payload_of_the_released_app_is_accepted(client, db):
    # Exactly what www/index.html (v50) sends: no extra or renamed fields.
    body = {"token": TOKEN, "lat": 51.9244, "lng": 4.4777,
            "wishlist": ["__very_rare__", "Jan-van-gent"], "max_dist": 0}
    assert client.post("/register-token", json=body).status_code == 200
    assert _stored(db)[0]["max_dist"] == 0


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "null", '"x"'])
def test_non_finite_coordinates_are_rejected(client, db, literal):
    raw = (f'{{"token": "{TOKEN}", "lat": {literal}, "lng": 5.1,'
           ' "wishlist": ["Kuifmees"], "max_dist": 50}')
    r = client.post("/register-token", content=raw,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert _stored(db) == []


@pytest.mark.parametrize("overrides", [
    {"lat": 90.1}, {"lat": -91}, {"lng": 180.5}, {"lng": -181},
    {"token": "short"}, {"token": "x" * 4097},
    {"max_dist": -1}, {"max_dist": 10**9}, {"max_dist": None},
    {"wishlist": ["a"] * 2001}, {"wishlist": ["x" * 121]}, {"wishlist": [""]},
    {"wishlist": "Kuifmees"}, {"wishlist": [1, 2]},
])
def test_out_of_bounds_fields_are_rejected(client, db, overrides):
    r = client.post("/register-token", json=subscriber(**overrides))
    assert r.status_code == 422
    assert _stored(db) == []


def test_validation_error_does_not_echo_the_input(client):
    r = client.post("/register-token", json=subscriber(token="secret-marker"))
    assert r.status_code == 422
    assert "secret-marker" not in r.text


def test_missing_and_malformed_bodies_are_rejected(client, db):
    assert client.post("/register-token", content=b"").status_code == 422
    assert client.post("/register-token", content=b"{not json").status_code == 422
    assert client.post("/register-token", json={"token": TOKEN}).status_code == 422
    assert _stored(db) == []


def test_oversized_body_is_refused(client, db):
    big = subscriber(wishlist=["x" * 100] * 1500)   # about 155 kB
    assert client.post("/register-token", json=big).status_code == 413
    assert _stored(db) == []


def test_oversized_body_without_content_length_is_refused(client, db):
    def chunks():
        yield b'{"token": "'
        for _ in range(200):
            yield b"x" * 1024
        yield b'"}'
    r = client.post("/register-token", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert _stored(db) == []


def test_reregistering_updates_the_same_row(client, db):
    client.post("/register-token", json=subscriber())
    client.post("/register-token", json=subscriber(lat=53.2, wishlist=["Kwak"]))
    rows = _stored(db)
    assert len(rows) == 1
    assert rows[0]["lat"] == 53.2
    assert json.loads(rows[0]["wishlist"]) == ["Kwak"]


def test_unregister_removes_the_row(client, db):
    client.post("/register-token", json=subscriber())
    assert client.delete(f"/register-token/{TOKEN}").status_code == 200
    assert _stored(db) == []
