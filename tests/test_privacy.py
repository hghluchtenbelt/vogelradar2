"""What the privacy page promises about stored registrations."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from conftest import TOKEN, subscriber

ROOT = Path(__file__).resolve().parent.parent
OTHER = "other-" + "z" * 60


def _tokens(db) -> set[str]:
    with db._connect() as conn:
        return {r[0] for r in conn.execute("SELECT fcm_token FROM push_subscribers")}


def _set_updated(db, token: str, days_ago: float) -> None:
    ts = (datetime.utcnow() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    with db._connect() as conn:
        conn.execute("UPDATE push_subscribers SET updated_at = ? WHERE fcm_token = ?",
                     (ts, token))
        conn.commit()


def test_startup_post_without_wishes_stores_nothing(client, db):
    # The released app posts token + GPS at startup, before any wish exists.
    r = client.post("/register-token", json=subscriber(wishlist=[]))
    assert r.status_code == 200
    assert _tokens(db) == set()


def test_removing_the_last_wish_deletes_the_registration_at_once(client, db):
    client.post("/register-token", json=subscriber())
    db.record_notification(TOKEN, "Kuifmees", 52.1, 5.1)
    assert _tokens(db) == {TOKEN}

    client.post("/register-token", json=subscriber(wishlist=[]))

    assert _tokens(db) == set()
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sent_notifications").fetchone()[0] == 0


def test_unused_registrations_are_removed_after_the_stated_period(client, db):
    client.post("/register-token", json=subscriber())
    client.post("/register-token", json=subscriber(token=OTHER))
    _set_updated(db, TOKEN, db.SUBSCRIBER_MAX_IDLE_DAYS + 1)
    _set_updated(db, OTHER, db.SUBSCRIBER_MAX_IDLE_DAYS - 1)

    assert db.prune_subscribers() == 1
    assert _tokens(db) == {OTHER}


def test_opening_the_app_keeps_a_registration_alive(client, db):
    client.post("/register-token", json=subscriber())
    _set_updated(db, TOKEN, db.SUBSCRIBER_MAX_IDLE_DAYS - 1)
    client.post("/register-token", json=subscriber())        # app opened again
    _set_updated_days = db.SUBSCRIBER_MAX_IDLE_DAYS + 5
    assert db.prune_subscribers(max_idle_days=_set_updated_days) == 0
    assert _tokens(db) == {TOKEN}


def test_scrape_run_applies_the_cleanup(db, monkeypatch):
    import updater

    db.upsert_push_subscriber(TOKEN, 52.09, 5.12, '["Kuifmees"]', 50)
    _set_updated(db, TOKEN, db.SUBSCRIBER_MAX_IDLE_DAYS + 1)
    monkeypatch.setattr(updater, "run_revalidation", lambda *a, **k: (0, 0, 0))
    monkeypatch.setattr(updater, "fetch_rare_birds", lambda **k: [])
    updater.run_update()
    assert _tokens(db) == set()


def _privacy_text() -> str:
    html = (ROOT / "privacy.html").read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_privacy_page_states_the_retention_the_code_applies(db):
    text = _privacy_text()
    assert f"na {db.SUBSCRIBER_MAX_IDLE_DAYS} dagen" in text
    assert f"na {db.NOTIFY_DEDUPE_HOURS} uur gewist" in text


def test_privacy_page_does_not_promise_what_the_server_cannot_see():
    text = _privacy_text()
    # Switching notifications off in the phone settings never reaches the
    # server, so the page must not claim an automatic deletion for it.
    assert "kan onze server dat niet zien" in text
    assert "je gegevens worden dan automatisch verwijderd" not in text
    assert "periodiek opgeschoond" not in text
