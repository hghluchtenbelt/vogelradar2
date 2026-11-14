"""/birds.json is served from memory with cache headers; /healthz is cheap."""
from __future__ import annotations

from pathlib import Path

import api

ROOT = Path(__file__).resolve().parent.parent


def _row(n: int, name: str = "Kuifmees") -> dict:
    return {
        "url": f"https://waarneming.nl/observation/{n}/", "bird_name": name,
        "location": "Utrecht - Griftpark", "date": "2999-01-01", "obs_time": "09:12",
        "count": 2, "photo": 1, "photo_url": "https://waarneming.nl/media/photo/1.jpg",
        "latitude": 52.1, "longitude": 5.1, "rarity": 3,
    }


def test_feed_keeps_the_shape_the_released_app_reads(client, db):
    db.insert_sightings([_row(284913201, "Jan-van-gent")])
    r = client.get("/birds.json")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    data = r.json()
    assert set(data) == {"lastUpdated", "sightings"}
    assert data["lastUpdated"].endswith("Z")
    assert data["sightings"] == [{
        "id": "284913201", "nl": "Jan-van-gent", "en": "", "sci": "",
        "rarity": "rare", "count": 2, "lat": 52.1, "lng": 5.1,
        "loc": "Utrecht - Griftpark", "date": "2999-01-01", "time": "09:12",
        "photo": True, "img": "https://waarneming.nl/media/photo/1.jpg",
        "url": "https://waarneming.nl/observation/284913201/",
    }]


def test_feed_is_built_once_per_ttl(client, db, monkeypatch):
    db.insert_sightings([_row(1)])
    calls = []
    real = api.get_sightings
    monkeypatch.setattr(api, "get_sightings", lambda: calls.append(1) or real())
    first = client.get("/birds.json")
    for _ in range(5):
        assert client.get("/birds.json").content == first.content
    assert len(calls) == 1


def test_feed_is_rebuilt_after_a_scrape_and_after_the_ttl(client, db, monkeypatch):
    db.insert_sightings([_row(1)])
    assert len(client.get("/birds.json").json()["sightings"]) == 1

    db.insert_sightings([_row(2)])
    assert len(client.get("/birds.json").json()["sightings"]) == 1     # still cached
    api._invalidate_feed()                                             # what the updater does
    assert len(client.get("/birds.json").json()["sightings"]) == 2

    db.insert_sightings([_row(3)])
    now = api.time.time()
    monkeypatch.setattr(api.time, "time", lambda: now + api._FEED_TTL + 1)
    assert len(client.get("/birds.json").json()["sightings"]) == 3


def test_unchanged_feed_answers_304(client, db):
    db.insert_sightings([_row(1)])
    first = client.get("/birds.json")
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == f"public, max-age={api._FEED_TTL}"

    for sent in (etag, "W/" + etag, f'"other", W/{etag}'):
        again = client.get("/birds.json", headers={"If-None-Match": sent})
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["etag"] == etag

    assert client.get("/birds.json", headers={"If-None-Match": '"stale"'}).status_code == 200

    db.insert_sightings([_row(2)])
    api._invalidate_feed()
    changed = client.get("/birds.json", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


def test_cross_origin_app_can_still_read_the_feed(client, db):
    # The Android app loads the feed from its own origin.
    r = client.get("/birds.json", headers={"Origin": "https://localhost"})
    assert r.headers["access-control-allow-origin"] == "*"


def test_healthz_does_not_build_the_feed(client, monkeypatch):
    monkeypatch.setattr(api, "get_sightings",
                        lambda: (_ for _ in ()).throw(AssertionError("feed built")))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz_fails_when_the_database_is_unusable(client, db, monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing-dir" / "x.db")
    with pytest.raises(Exception):  # noqa: B017, PT011 (any failure marks the container unhealthy)
        client.get("/healthz")


def test_container_healthcheck_uses_healthz():
    dockerfile = (ROOT / "Dockerfile").read_text()
    line = next(ln for ln in dockerfile.splitlines() if "urlopen" in ln)
    assert "/healthz" in line
    assert "birds.json" not in line
