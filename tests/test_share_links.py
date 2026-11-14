"""Shareable sighting links: /waarneming/<id> with preview tags in the HTML."""
from __future__ import annotations

import re

import pytest


def _row(n: int, **over) -> dict:
    row = {
        "url": f"https://waarneming.nl/observation/{n}/", "bird_name": "Dunsnavelnotenkraker",
        "location": "Ede - Planken Wambuis (GE)", "date": "2026-11-13", "obs_time": "11:31",
        "count": 1, "photo": 1, "photo_url": "https://waarneming.nl/media/photo/99.jpg",
        "latitude": 52.03667, "longitude": 5.78114, "rarity": 4,
    }
    row.update(over)
    return row


def _meta(html: str, prop: str) -> str:
    m = re.search(rf'<meta (?:name|property)="{re.escape(prop)}" content="([^"]*)"', html)
    assert m, f"missing meta {prop}"
    return m.group(1)


def test_shared_link_describes_the_sighting(client, db):
    db.insert_sightings([_row(123456)])
    r = client.get("/waarneming/123456")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "<title>Dunsnavelnotenkraker · Ede - Planken Wambuis (GE) | Vogelradar</title>" in html
    assert _meta(html, "og:title") == "Dunsnavelnotenkraker · Ede - Planken Wambuis (GE) | Vogelradar"
    assert _meta(html, "description") == \
        "Zeer zeldzaam, gezien op 13 november 2026 om 11:31. Bekijk de plek op de kaart van Vogelradar."
    assert _meta(html, "og:url") == "https://vogel-radar.nl/waarneming/123456"
    assert _meta(html, "og:image") == "https://waarneming.nl/media/photo/99.jpg?w=600"
    assert _meta(html, "robots") == "noindex"
    assert '<link rel="canonical" href="https://vogel-radar.nl/waarneming/123456">' in html
    # Exactly one title and one og:title: the generic block was replaced, not appended to.
    assert html.count("<title>") == 1 and html.count('property="og:title"') == 1
    # It is still the whole app.
    assert 'id="map"' in html and "/species_data.js" in html


def test_sighting_without_photo_uses_the_site_icon(client, db):
    db.insert_sightings([_row(5, photo=0, photo_url="", obs_time="")])
    html = client.get("/waarneming/5").text
    assert _meta(html, "og:image") == "https://vogel-radar.nl/icon.png"
    assert _meta(html, "description").startswith("Zeer zeldzaam, gezien op 13 november 2026.")


def test_scraped_text_cannot_break_out_of_the_head(client, db):
    evil = '"><script>alert(1)</script><meta x="'
    db.insert_sightings([_row(7, bird_name=evil, location="</title><script>alert(2)</script>",
                              photo_url='https://waarneming.nl/x.jpg"><script>alert(3)</script>')])
    html = client.get("/waarneming/7").text
    head = html[:html.index("</head>")]
    assert "<script>alert" not in head
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in head
    assert head.count("<title>") == 1


def test_non_https_photo_is_not_used_as_preview_image(client, db):
    db.insert_sightings([_row(8, photo_url="javascript:alert(1)")])
    assert _meta(client.get("/waarneming/8").text, "og:image") == "https://vogel-radar.nl/icon.png"


def test_unknown_sighting_still_serves_the_app_but_as_404(client, db):
    r = client.get("/waarneming/999999")
    assert r.status_code == 404
    assert 'id="map"' in r.text
    assert "<title>Waarneming niet meer beschikbaar | Vogelradar</title>" in r.text
    assert _meta(r.text, "robots") == "noindex"


@pytest.mark.parametrize("bad", ["abc", "12abc", "1" * 13, "-1", "1.5", "%3Cscript%3E"])
def test_only_numeric_ids_are_routed(client, db, bad):
    r = client.get(f"/waarneming/{bad}")
    assert r.status_code == 404
    assert "<html" not in r.text


def test_home_page_keeps_its_generic_head(client):
    html = client.get("/").text
    assert _meta(html, "og:url") == "https://vogel-radar.nl/"
    assert 'name="robots"' not in html


def test_dutch_dates():
    import api

    assert api._date_nl("2026-03-01", "07:05") == "1 maart 2026 om 07:05"
    assert api._date_nl("2026-12-31", "") == "31 december 2026"
    assert api._date_nl("", "07:05") == ""
    assert api._date_nl("2026-13-01", "") == ""
