"""The public page: language, zoom, preview tags, icon and manifest."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "vogelradar.html").read_text(encoding="utf-8")


def _meta(prop: str) -> str:
    m = re.search(rf'<meta (?:name|property)="{re.escape(prop)}" content="([^"]*)"', HTML)
    assert m, f"missing meta {prop}"
    return m.group(1)


def test_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"


def test_page_declares_dutch():
    assert '<html lang="nl">' in HTML


def test_page_zoom_is_not_disabled():
    viewport = _meta("viewport")
    assert "user-scalable" not in viewport
    assert "maximum-scale" not in viewport


def test_description_and_preview_tags():
    assert 40 <= len(_meta("description")) <= 200
    assert _meta("og:title") and _meta("og:description")
    assert _meta("og:url") == "https://vogel-radar.nl/"
    assert _meta("og:image") == "https://vogel-radar.nl/icon.png"
    assert _meta("twitter:card") == "summary"
    assert '<link rel="canonical" href="https://vogel-radar.nl/">' in HTML


def test_icon_and_manifest_are_linked_and_served(client):
    assert '<link rel="icon" type="image/png" href="/icon.png">' in HTML
    assert '<link rel="manifest" href="/manifest.webmanifest">' in HTML
    for path in ("/icon.png", "/favicon.ico"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200
    data = m.json()
    assert data["lang"] == "nl" and data["start_url"] == "/"
    assert data["icons"][0]["src"] == "/icon.png"


def test_assets_use_absolute_paths():
    # The page is also served under /waarneming/<id>; relative paths would break there.
    assert 'src="icon.png"' not in HTML
    assert 'src="/species_data.js"' in HTML


def test_scraped_text_is_escaped_before_it_reaches_the_dom():
    # The two fixes that only existed in the Android app.
    assert 'data-id="${esc(s.id)}"' in HTML
    assert "const imgUrl = safeUrl(" in HTML
    # Species rows in the wish list are escaped as well.
    assert 'data-nl="${esc(nl)}"' in HTML and '${esc(sci)}' in HTML


def test_no_em_dash_in_visible_page_text():
    body = HTML[HTML.index("<body>"):HTML.index("<script src=")]
    assert "—" not in body


def test_page_explains_itself_and_links_onwards():
    about = HTML[HTML.index('id="amod"'):HTML.index('<script src="/species_data.js">')]
    assert 'href="https://waarneming.nl"' in about            # source credit
    assert "geen onderdeel van waarneming.nl" in about
    for href in ("/privacy", "/stats", "/voorspelling",
                 "https://play.google.com/store/apps/details?id=com.vogelradar.app"):
        assert f'href="{href}"' in about


def test_website_wish_list_does_not_promise_background_alerts():
    wish = HTML[HTML.index('id="wmod"'):HTML.index('id="amod"')]
    assert "zolang deze pagina open staat" in wish
    assert "Android-app" in wish
