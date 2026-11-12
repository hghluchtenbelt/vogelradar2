"""The species universe: every species that waarneming.nl classifies as
"zeldzaam" or "zeer zeldzaam", from the species export in soortenlijst/.

Rules:
* type "soort" with a rare class: one layer;
* "ondersoort" or "synoniem" with a rare class: merged into the parent when
  the parent is rare itself (Dunsnavelnotenkraker into Notenkraker), kept as
  its own layer when the parent is common (Siberische Tjiftjaf, Bonte Kraai);
* hybrids, aggregate taxa ("verzamelsoort"), forms and varieties are skipped.
Species that the live feed marks rare but the export does not (the export is
a snapshot; rarity icons on observations can differ) are added by the
caller with source "feed".
"""
from __future__ import annotations

import csv
import glob
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RARE_CLASSES = ("zeldzaam", "zeer zeldzaam")
SKIP_TYPES = {"hybride", "verzamelsoort", "forma", "variëteit"}


def export_path() -> str | None:
    files = sorted(glob.glob(os.path.join(os.path.dirname(HERE), "soortenlijst", "*.csv")))
    return files[-1] if files else None


def load_universe(path: str | None = None) -> dict[str, dict]:
    """Canonical label -> {label, sci, rarity_class, family, status, waarneming_id, names}."""
    path = path or export_path()
    if not path:
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    byid = {r["id"]: r for r in rows}
    out: dict[str, dict] = {}
    merged: list[tuple[str, str]] = []
    for r in rows:
        if r["rarity"] not in RARE_CLASSES or r["type"] in SKIP_TYPES:
            continue
        if r["type"] in ("ondersoort", "synoniem"):
            parent = byid.get(r["parent species id"])
            if parent and parent["rarity"] in RARE_CLASSES and parent["type"] not in SKIP_TYPES:
                merged.append((r["name"], parent["name"]))
                continue
        out[r["name"]] = {"label": r["name"], "sci": r["scientific name"], "rarity_class": r["rarity"],
                          "family": r["family"], "status": r["status"], "waarneming_id": r["id"],
                          "names": {r["name"]}, "source": "export"}
    for raw, parent in merged:
        if parent in out:
            out[parent]["names"].add(raw)
    # "X (ssp y)" rows next to a rare "X" row describe the same layer.
    for label in [l for l in out if " (ssp " in l]:
        base = label.split(" (ssp ")[0]
        if base in out:
            out[base]["names"] |= out.pop(label)["names"]
    return out
