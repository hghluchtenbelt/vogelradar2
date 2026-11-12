"""Forecast pipeline for the web container: rebuild every species layer from
the live sightings database after each scrape, and keep a daily archive.

Called from api.py's updater thread when FORECAST_ENABLED=1 (see api.py), or
by hand:  python forecast/pipeline.py [--source db|snapshots]

Environment:
  FORECAST_OUT_DIR   where layers go (default forecast/out; in the container a
                     folder on the /data volume so it is writable and persists)
  FORECAST_DATA_DIR  wind cache and daily archive (default FORECAST_OUT_DIR/../data)
  VOGELRADAR_DB_PATH the sightings database (read through database.get_sightings)

Needs numpy and Pillow only (the grid file already holds the coast fields).
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

OUT_DIR = os.environ.get("FORECAST_OUT_DIR") or os.path.join(HERE, "out")
DATA_DIR = os.environ.get("FORECAST_DATA_DIR") or os.path.join(os.path.dirname(OUT_DIR), "data")
WIND_MAX_AGE_H = 6


def _db_reports() -> list[dict]:
    """Rare and very rare rows of the last 7 days from the live database, in feed shape."""
    from database import get_sightings
    from model import _norm_db
    rows = get_sightings()
    out = []
    for s in rows:
        if int(s.get("rarity") or 3) >= 3:
            r = _norm_db(s)
            if r:
                out.append(r)
    return out


def _archive(reports: list[dict]) -> str | None:
    """One gzipped file per day with every rare report known today (input for backtests)."""
    adir = os.path.join(DATA_DIR, "archive")
    os.makedirs(adir, exist_ok=True)
    path = os.path.join(adir, f"{date.today().isoformat()}.json.gz")
    if os.path.exists(path):
        return None
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sightings": reports}, f)
    return path


KEEP = {"index.json", "calibration.json", "backtest-summary.csv", "pipeline-status.json", "stay-model.json", "gemeentes.json"}


def _cleanup(slugs: set[str]) -> None:
    """Remove layers of species that left the registry (renamed or merged)."""
    for name in os.listdir(OUT_DIR):
        if name in KEEP:
            continue
        stem = name.rsplit(".", 1)[0]
        stem = stem[:-9] if stem.endswith("-backtest") else stem
        stem = stem[:-6] if stem.endswith("-nieuw") else stem
        if stem not in slugs and stem != "alle":
            try:
                os.remove(os.path.join(OUT_DIR, name))
            except OSError:
                pass


def _merge_archive(reports: list[dict]) -> list[dict]:
    """Older archived rows (beyond the live 7-day window) feed the stay model."""
    import glob
    by_url = {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "archive", "*.json.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for r in json.load(f).get("sightings", []):
                by_url[r["url"]] = r
    for r in reports:
        by_url[r["url"]] = r
    return list(by_url.values())


def run(source: str = "db", verbose: bool = False) -> dict:
    import wind as windmod
    import model
    model.OUT_DIR = OUT_DIR
    windmod.CACHE_DIR = os.path.join(DATA_DIR, "wind")
    t0 = time.time()
    if source == "db":
        reports = _db_reports()
        _archive(reports)
        reports = _merge_archive(reports)
    else:
        reports = model.load_sightings()
    if not reports:
        return {"species": 0, "with_map": 0, "seconds": 0, "note": "no reports"}
    grid = model.load_grid()
    registry = model.species_registry(reports)
    latest = max(model.parse_date(r["date"]) for r in reports)
    # wind: reuse the cache unless it is older than WIND_MAX_AGE_H
    wdata = windmod.load(need_until=latest + timedelta(days=1), allow_fetch=False)
    fetched = wdata and (datetime.now(timezone.utc) - datetime.fromisoformat(wdata["fetched_at"])) < timedelta(hours=WIND_MAX_AGE_H)
    if not fetched:
        try:
            wdata = windmod.fetch()
        except Exception as exc:
            if verbose:
                print("wind fetch failed:", exc)
    stay_model = model.fit_stay_model(reports, registry)
    acc = model.new_accumulator(grid)
    entries = model.run_species(reports, grid, registry, sorted(registry), latest, wdata, verbose=verbose, stay_model=stay_model, acc=acc)
    entries.insert(0, model.index_entry(model.write_combined(acc, grid, latest)))
    model.write_index(entries)
    # municipality outlines for the page (tap selects the area)
    import shutil
    shutil.copyfile(os.path.join(os.path.dirname(HERE), "gemeentes.geojson"), os.path.join(OUT_DIR, "gemeentes.json"))
    _cleanup(set(registry))
    with_map = sum(1 for e in entries if e["has_map"])
    result = {"species": len(entries), "with_map": with_map, "reports": len(reports), "seconds": round(time.time() - t0, 1),
              "wind": bool(wdata), "stay_model_n": stay_model.get("n"), "out_dir": OUT_DIR}
    with open(os.path.join(OUT_DIR, "pipeline-status.json"), "w", encoding="utf-8") as f:
        json.dump({**result, "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, f)
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="db", choices=["db", "snapshots"])
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    print(run(a.source, verbose=not a.quiet))
