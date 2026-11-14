"""The rate limiter counts per visitor, not per proxy."""
from __future__ import annotations

from conftest import subscriber

import api


def _post(client, ip: str | None, n: int = 1) -> list[int]:
    headers = {"CF-Connecting-IP": ip} if ip else {}
    return [client.post("/register-token", json=subscriber(), headers=headers).status_code
            for _ in range(n)]


def test_one_visitor_is_limited(client):
    codes = _post(client, "203.0.113.7", api._RATE_MAX + 1)
    assert codes[:-1] == [200] * api._RATE_MAX
    assert codes[-1] == 429


def test_visitors_behind_the_same_proxy_have_separate_budgets(client):
    # Every request arrives from the same socket peer (the test client),
    # exactly like production behind Traefik.
    assert _post(client, "203.0.113.7", api._RATE_MAX + 1)[-1] == 429
    assert _post(client, "203.0.113.8") == [200]
    assert _post(client, "2001:db8::1") == [200]


def test_without_proxy_header_the_socket_peer_is_used(client):
    assert _post(client, None, api._RATE_MAX + 1)[-1] == 429
    assert _post(client, "203.0.113.9") == [200]


def test_unregister_shares_the_budget(client):
    assert _post(client, "203.0.113.7", api._RATE_MAX)[-1] == 200
    r = client.delete("/register-token/" + "t" * 40,
                      headers={"CF-Connecting-IP": "203.0.113.7"})
    assert r.status_code == 429


def test_tracked_addresses_are_bounded(client, monkeypatch):
    monkeypatch.setattr(api, "_RATE_MAX_KEYS", 50)
    for i in range(200):
        assert api._allow_rate(f"198.51.100.{i}")
    assert len(api._rate_limit) <= 51


def test_expired_entries_free_the_budget(client, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(api.time, "time", lambda: clock[0])
    for _ in range(api._RATE_MAX):
        assert api._allow_rate("203.0.113.7")
    assert not api._allow_rate("203.0.113.7")
    clock[0] += api._RATE_WINDOW + 1
    assert api._allow_rate("203.0.113.7")
