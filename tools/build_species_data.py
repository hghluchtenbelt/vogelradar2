"""Regenerate species_data.js (the wish-list species) from the waarneming.nl
species export in soortenlijst/.

    python tools/build_species_data.py

Entries are [Dutch name, scientific name, rarity code] for every species,
plus [name, scientific name, rarity code, parent species] for every
subspecies or synonym that has a Dutch name of its own (Dunsnavelnotenkraker,
Bonte Kraai), so those can be wished for specifically. A wish for the parent
species also matches its subspecies (see species.wish_keys).

Copy the result to the Android app (www/species_data.js) afterwards; the app
bundles its own copy.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import species

CODE_ORDER = "vruc"
_SSP_WORD_RE = re.compile(r"\bssp\b")   # not the "ssp" inside Glansspreeuw


def _has_own_dutch_name(row: dict) -> bool:
    name = row["name"]
    if _SSP_WORD_RE.search(name) or "(" in name or "/" in name:
        return False
    words = name.split()
    # A scientific name used as a synonym ("Parus ater"): second word lowercase.
    return not (len(words) > 1 and words[1][:1].islower())


def build() -> list[list[str]]:
    rows = species.export_rows()
    by_id = {r["id"]: r for r in rows}
    entries: list[list[str]] = []
    for r in rows:
        code = species.RARITY_CODE.get(r["rarity"])
        if not code:
            continue
        if r["type"] == "soort":
            entries.append([r["name"], r["scientific name"], code])
        elif r["type"] in ("ondersoort", "synoniem") and _has_own_dutch_name(r):
            parent = by_id.get(r["parent species id"])
            if parent and parent["type"] == "soort":
                entries.append([r["name"], r["scientific name"], code, parent["name"]])
    entries.sort(key=lambda e: (CODE_ORDER.index(e[2]), e[0]))
    return entries


def main() -> None:
    entries = build()
    body = ", ".join(json.dumps(e, ensure_ascii=False) for e in entries)
    out = ROOT / "species_data.js"
    out.write_text(f"const SPECIES=[{body}];", encoding="utf-8")
    subs = sum(1 for e in entries if len(e) == 4)
    print(f"{out.name}: {len(entries)} entries ({len(entries) - subs} species, {subs} subspecies)")


if __name__ == "__main__":
    main()
