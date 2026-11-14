"""/places.json: municipalities for the manual location picker."""
from __future__ import annotations

import gemeente


def test_places_cover_every_municipality(client):
    r = client.get("/places.json")
    assert r.status_code == 200
    assert "max-age" in r.headers["cache-control"]
    places = r.json()["places"]
    names = [p[0] for p in places]
    assert len(names) == len(set(names))
    assert len(names) >= 342
    for must in ("Zwolle", "Utrecht", "Texel", "Den Haag", "'s-Gravenhage", "Den Bosch"):
        assert must in names


def test_centres_lie_inside_the_netherlands_and_their_own_municipality():
    for name, lat, lng in gemeente.places():
        assert 50.7 < lat < 53.6 and 3.3 < lng < 7.3, (name, lat, lng)
    official = {a[0] for a in gemeente._areas}
    inside = sum(1 for name, lat, lng in gemeente.places()
                 if name in official and gemeente.gemeente_for(lat, lng) == name)
    # A centre can fall just outside a crescent-shaped municipality; it must be rare.
    assert inside >= 0.9 * len(official), inside


def test_known_centres_are_about_right():
    by_name = {n: (lat, lng) for n, lat, lng in gemeente.places()}
    for name, (lat, lng) in {"Zwolle": (52.51, 6.09), "Utrecht": (52.09, 5.11),
                             "Maastricht": (50.85, 5.69), "Den Haag": (52.07, 4.30)}.items():
        assert abs(by_name[name][0] - lat) < 0.08 and abs(by_name[name][1] - lng) < 0.12, name
