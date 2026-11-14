"""Wish-list matching across subspecies names, and the species list file."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_notifications import GOOD, SIGHTING, fcm  # noqa: F401 (fixture)

import notifications
import species
import updater

ROOT = Path(__file__).resolve().parent.parent


def _species_list() -> list[list[str]]:
    text = (ROOT / "species_data.js").read_text(encoding="utf-8")
    assert text.startswith("const SPECIES=") and text.endswith(";")
    return json.loads(text[len("const SPECIES="):-1])


@pytest.mark.parametrize("reported, wish", [
    ("Kuifmees", "Kuifmees"),
    ("Bonte Kraai ssp cornix", "Bonte Kraai"),          # ssp suffix
    ("Wintertaling ssp crecca", "Wintertaling"),
    ("Bonte Kraai (ssp cornix)", "Bonte Kraai"),        # export spelling
    ("Dunsnavelnotenkraker", "Notenkraker"),            # named subspecies -> parent
    ("Dunsnavelnotenkraker", "Dunsnavelnotenkraker"),
    ("Siberische Tjiftjaf", "Tjiftjaf"),
    ("Zwarte Rotgans", "Rotgans"),
    ("Jan-van-gent", "Jan-van-gent"),
])
def test_report_satisfies_wish(reported, wish):
    assert wish in species.wish_keys(reported)


@pytest.mark.parametrize("reported, wish", [
    ("Notenkraker", "Dunsnavelnotenkraker"),   # a wish for the subspecies is specific
    ("Rotgans", "Zwarte Rotgans"),
    ("Parkeend", "Wilde Eend"),                # domestic forms are not the wild species
    ("Hybride Bonte Kraai x Zwarte Kraai", "Zwarte Kraai"),
    ("Kuifmees", "Kuifeend"),
])
def test_report_does_not_satisfy_wish(reported, wish):
    assert wish not in species.wish_keys(reported)


def test_push_matches_subspecies_report_for_parent_wish(db, fcm):  # noqa: F811
    db.upsert_push_subscriber(GOOD, 52.09, 5.12, '["Notenkraker"]', 50)
    notifications.send_push_notifications(
        [{**SIGHTING, "bird_name": "Dunsnavelnotenkraker"}])
    assert fcm.tokens() == [GOOD]
    assert fcm.sent[0].data["bird_name"] == "Dunsnavelnotenkraker"


def test_species_list_is_generated_from_the_export():
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import build_species_data

    assert _species_list() == build_species_data.build()


def test_species_list_shape():
    entries = _species_list()
    names = [e[0] for e in entries]
    assert len(names) == len(set(names))
    assert all(len(e) in (3, 4) and e[2] in "vruc" for e in entries)
    by_name = {e[0]: e for e in entries}
    assert by_name["Jan-van-gent"] == ["Jan-van-gent", "Morus bassanus", "u"]
    # Named subspecies are selectable and point at a species in the list.
    assert by_name["Dunsnavelnotenkraker"][3] == "Notenkraker"
    assert by_name["Bonte Kraai"][3] == "Zwarte Kraai"
    assert all(e[3] in by_name and len(by_name[e[3]]) == 3
               for e in entries if len(e) == 4)
    # No "ssp" spellings, scientific-name synonyms or domestic forms.
    assert not any(re.search(r"\bssp\b", n) or "(" in n for n in names)
    assert "Parus ater" not in by_name and "Parkeend" not in by_name


def test_truncated_names_map_back_to_full_names():
    fixes = species.truncated_name_fixes()
    assert fixes["Jan"] == "Jan-van-gent"
    assert fixes["Zwarte Zee"] == "Zwarte Zee-eend"
    assert fixes["Grote Zee"] == "Grote Zee-eend"
    real = {species.normalise(r["name"]) for r in species.export_rows()}
    assert not (set(fixes) & real)              # never renames a real name
    assert all(v.startswith(k) for k, v in fixes.items())


def test_repair_renames_stored_rows_once(db):
    rows = [
        {"url": "https://waarneming.nl/observation/1/", "bird_name": "Jan"},
        {"url": "https://waarneming.nl/observation/2/", "bird_name": "Zwarte Zee"},
        {"url": "https://waarneming.nl/observation/3/", "bird_name": "Kuifmees"},
    ]
    db.insert_sightings([
        {**r, "location": "x", "date": "2026-11-14", "obs_time": "", "count": 1,
         "photo": 0, "latitude": 52.0, "longitude": 5.0, "rarity": 1} for r in rows
    ])
    assert updater.repair_truncated_names() == 2
    with db._connect() as conn:
        names = sorted(r[0] for r in conn.execute("SELECT bird_name FROM sightings"))
    assert names == ["Jan-van-gent", "Kuifmees", "Zwarte Zee-eend"]
    assert updater.repair_truncated_names() == 0     # guarded by a meta flag
