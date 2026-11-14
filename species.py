"""Species names: which wish-list entries a reported name satisfies.

waarneming.nl reports many birds under a subspecies name ("Bonte Kraai
ssp cornix", "Dunsnavelnotenkraker") that is not a species of its own, so
an exact string comparison with the wish list never matched them. The
species export in soortenlijst/ links every subspecies and synonym to its
parent species; that link is used here, and by tools/build_species_data.py
to make the named subspecies selectable in the wish list.
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

EXPORT_DIR = Path(__file__).parent / "soortenlijst"

# Rarity class in the export -> code used in species_data.js
RARITY_CODE = {"zeer zeldzaam": "v", "zeldzaam": "r",
               "vrij algemeen": "u", "algemeen": "c"}

_SSP_PAREN_RE = re.compile(r"\(ssp ([^)]*)\)")
_SSP_SUFFIX_RE = re.compile(r"\s+ssp\b.*$")


def normalise(name: str) -> str:
    """The export writes "X (ssp y)", observation pages "X ssp y"."""
    return " ".join(_SSP_PAREN_RE.sub(r"ssp \1", name).split())


def base_name(name: str) -> str:
    """"Bonte Kraai ssp cornix" -> "Bonte Kraai"."""
    return _SSP_SUFFIX_RE.sub("", normalise(name))


@lru_cache(maxsize=1)
def export_rows() -> tuple[dict, ...]:
    files = sorted(EXPORT_DIR.glob("*.csv"))
    if not files:
        return ()
    with open(files[-1], encoding="utf-8", newline="") as f:
        return tuple(csv.DictReader(f))


@lru_cache(maxsize=1)
def parents() -> dict[str, str]:
    """Parent species by (normalised) subspecies or synonym name. Domestic
    forms (Parkeend, Stadsduif) are left out on purpose: a wish for Wilde
    Eend is not a wish for park ducks."""
    rows = export_rows()
    by_id = {r["id"]: r for r in rows}
    out: dict[str, str] = {}
    for r in rows:
        if r["type"] not in ("ondersoort", "synoniem"):
            continue
        parent = by_id.get(r["parent species id"])
        if parent and parent["type"] == "soort":
            out[normalise(r["name"])] = parent["name"]
    return out


def wish_keys(bird_name: str) -> set[str]:
    """Every wish-list entry that a sighting with this name satisfies: the
    name itself, the name without its "ssp" suffix, and the parent species
    of either."""
    name = normalise(bird_name)
    base = base_name(name)
    keys = {bird_name, name, base}
    for n in (name, base):
        parent = parents().get(n)
        if parent:
            keys.add(parent)
    return keys


def truncated_name_fixes() -> dict[str, str]:
    """Names the old parser produced by cutting at the first hyphen
    ("Jan" for Jan-van-gent), mapped back to the full name. A prefix that
    is a real name itself, or that several names share without exactly
    one of them being a full species, is left alone."""
    rows = export_rows()
    real = {normalise(r["name"]) for r in rows}
    candidates: dict[str, list[dict]] = {}
    for r in rows:
        name = normalise(r["name"])
        if "-" not in name:
            continue
        prefix = name.split("-")[0].strip()
        if prefix and prefix not in real:
            candidates.setdefault(prefix, []).append(r)
    fixes: dict[str, str] = {}
    for prefix, rs in candidates.items():
        if len(rs) > 1:
            rs = [r for r in rs if r["type"] == "soort"]
        if len(rs) == 1:
            fixes[prefix] = normalise(rs[0]["name"])
    return fixes
