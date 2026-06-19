"""FastAPI backend — serves /birds.json for the Vogelradar frontend."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from database import get_sightings, get_latest_update, init_db

# How often to re-scrape waarneming.nl in the background (seconds).
SCRAPE_INTERVAL = 60 * 60   # 1 hour — change to e.g. 30*60 for 30 min

app = FastAPI(title="Vogelradar")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
init_db()


def _background_updater() -> None:
    """Run the updater once per SCRAPE_INTERVAL, forever, in a daemon thread."""
    from updater import run_update
    while True:
        time.sleep(SCRAPE_INTERVAL)
        try:
            new, total = run_update()
            print(f"[updater] {new} new / {total} scraped", flush=True)
        except Exception as exc:
            print(f"[updater] error: {exc}", flush=True)


# Start background updater when the server starts (daemon=True means it
# won't block a clean shutdown).
_t = threading.Thread(target=_background_updater, daemon=True)
_t.start()

_HERE = Path(__file__).parent

# Integer rarity (scraped) → string rarity (HTML template)
_RARITY = {1: "common", 2: "uncommon", 3: "rare", 4: "very_rare"}
_OBS_RE = re.compile(r"/observation/(\d+)/")


def _to_sighting(row: dict) -> dict:
    m = _OBS_RE.search(row["url"])
    return {
        "id": m.group(1) if m else row["url"],
        "nl": row["bird_name"],
        "en": "",
        "sci": "",
        "rarity": _RARITY.get(row["rarity"], "rare"),
        "count": row.get("count") or 1,
        "lat": row["latitude"],
        "lng": row["longitude"],
        "loc": row.get("location") or "",
        "date": row.get("date") or "",
        "time": row.get("obs_time") or "",
        "photo": bool(row.get("photo")),
        "url": row["url"],
    }


@app.get("/birds.json")
def birds_json():
    rows = get_sightings()
    last = get_latest_update()
    return {
        "lastUpdated": (last + "Z") if last and not last.endswith("Z")
        else (last or ""),
        "sightings": [_to_sighting(r) for r in rows],
    }


@app.get("/species_data.js")
def species_data():
    return FileResponse(_HERE / "species_data.js", media_type="application/javascript")

@app.get("/privacy")
def privacy():
    return FileResponse(_HERE / "privacy.html", media_type="text/html")

@app.get("/icon.png")
def icon():
    return FileResponse(_HERE / "icon.png", media_type="image/png")

@app.get("/")
def index():
    return FileResponse(_HERE / "vogelradar.html")
