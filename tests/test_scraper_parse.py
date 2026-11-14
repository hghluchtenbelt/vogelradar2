"""Parsing an observation page. The HTML is a hand-written minimal copy of
the waarneming.nl structure (no real observation or observer data)."""
from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

import scraper

PAGE = """
<html><head>
<title>{title}</title>
</head><body>
{heading}
<table>
<tr><th><i class="fas fa-map-marker-alt fa-fw" title="locatie"></i></th>
    <td><a href="/locations/1/"> Texel - De Cocksdorp (NH) </a></td></tr>
<tr><th><i class="fas fa-calendar-alt fa-fw" title="datum"></i></th>
    <td>2026-11-13 12:11</td></tr>
<tr><th><i class="fas fa-hashtag fa-fw" title="aantal"></i></th><td>3 ter plaatse</td></tr>
</table>
<i class="fas fa-hexagon rare-3 fa-fw"></i>
<span class="teramap-coordinates-coords">53.01301, 4.78133</span>
</body></html>
"""


def _parse(title: str, heading: str = "") -> dict:
    html = PAGE.format(title=title, heading=heading)
    obs = scraper._parse_observation(
        BeautifulSoup(html, "lxml"), "https://waarneming.nl/observation/1/")
    assert obs is not None
    return obs


def _heading(name: str) -> str:
    return (f'<h1><a class="mod-stealth" href="/species/1/observations/">'
            f'<span class="species-common-name">{name}</span></a></h1>')


@pytest.mark.parametrize("name, sci", [
    ("Jan-van-gent", "Morus bassanus"),
    ("Zwarte Zee-eend", "Melanitta nigra"),
    ("Grote Zee-eend", "Melanitta fusca"),
    ("Amerikaanse Grote Zee-eend", "Melanitta deglandi"),
    ("Sint-Helenafazantje", "Estrilda astrild"),
    ("Kuifmees", "Lophophanes cristatus"),
    ("Bonte Kraai ssp cornix", "Corvus corone cornix"),
    ("Hybride Kleine Topper x Kuifeend", "Aythya affinis x Aythya fuligula"),
    ("Cetti's Zanger", "Cettia cetti"),
])
def test_species_name_is_not_cut_at_a_hyphen(name, sci):
    # The real title has a line break before the site name.
    title = f"{name} - {sci}\n        - Waarneming.nl"
    assert _parse(title, _heading(name))["bird_name"] == name   # name element
    assert _parse(title)["bird_name"] == name                   # title fallback


def test_name_element_wins_over_the_title():
    obs = _parse("Iets anders - Aliud - Waarneming.nl", _heading("Jan-van-gent"))
    assert obs["bird_name"] == "Jan-van-gent"


def test_heading_is_the_last_resort():
    html = PAGE.format(title="", heading="<h1> Jan-van-gent </h1>")
    obs = scraper._parse_observation(BeautifulSoup(html, "lxml"), "u")
    assert obs["bird_name"] == "Jan-van-gent"


def test_missing_name_falls_back_to_onbekend():
    html = PAGE.format(title="", heading="").replace("<title></title>", "")
    obs = scraper._parse_observation(BeautifulSoup(html, "lxml"), "u")
    assert obs["bird_name"] == "Onbekend"


def test_other_fields_still_parse():
    obs = _parse("Jan-van-gent - Morus bassanus - Waarneming.nl", _heading("Jan-van-gent"))
    assert obs["location"] == "Texel - De Cocksdorp (NH)"
    assert (obs["date"], obs["obs_time"]) == ("2026-11-13", "12:11")
    assert obs["count"] == 3
    assert obs["rarity"] == 3
    assert (obs["latitude"], obs["longitude"]) == (53.01301, 4.78133)
