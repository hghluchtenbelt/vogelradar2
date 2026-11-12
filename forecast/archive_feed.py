"""Archive the public Vogelradar feed (birds.json) as a gzipped snapshot.

Production prunes sightings after 15 days, so every evaluation of the
forecast depends on snapshots taken while the data were still there.
Saves forecast/data/snapshots/<UTC timestamp>.json.gz, skipping the
download result when lastUpdated equals the newest stored snapshot.

Run daily (cron on a laptop, or a scheduled task):
    .venv/bin/python forecast/archive_feed.py
Standard library only.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import sys
import urllib.request

FEED_URL = os.environ.get("VOGELRADAR_FEED_URL", "https://vogel-radar.nl/birds.json")
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "snapshots")


def latest_marker() -> str | None:
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.json.gz")))
    if not files:
        return None
    with gzip.open(files[-1], "rt", encoding="utf-8") as f:
        return json.load(f).get("lastUpdated")


def main() -> int:
    os.makedirs(SNAP_DIR, exist_ok=True)
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "vogelradar-forecast-archive/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    data = json.loads(raw)
    marker = data.get("lastUpdated")
    if marker and marker == latest_marker():
        print("feed unchanged since last snapshot", marker)
        return 0
    stamp = (marker or "").replace(":", "").replace("-", "")[:13] or "unknown"
    # e.g. 2026-10-24T1054 from 2026-10-24T10:54:07Z
    name = f"{marker[:10]}T{marker[11:13]}{marker[14:16]}" if marker else stamp
    path = os.path.join(SNAP_DIR, f"{name}.json.gz")
    with gzip.open(path, "wb") as f:
        f.write(raw)
    print(f"saved {path} ({len(data.get('sightings', []))} sightings, lastUpdated {marker})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
