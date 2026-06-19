"""
Standalone updater — run periodically (e.g. every 30 min via cron):

    python updater.py

Per-rarity time windows:
  First run  : zeldzaam=7d, vrij zeldzaam=3d, vrij algemeen=1d
  Incremental: zeldzaam=2d, vrij zeldzaam=2d, vrij algemeen=1d
Entries older than 15 days are pruned after each run.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from database import (
    init_db, insert_sightings, is_empty,
    get_all_urls, prune_old_sightings,
)
from scraper import fetch_rare_birds


def run_update(
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[int, int]:
    """Scrape and persist new sightings. Returns (new_count, total_scraped)."""
    init_db()
    first_run = is_empty()
    known_urls: set[str] = set() if first_run else get_all_urls()
    days_back_by_rarity = (
        {3: 7, 2: 3, 1: 1} if first_run else {3: 2, 2: 2, 1: 1}
    )

    total_new = 0
    total_scraped = 0

    def _flush(batch: list[dict]) -> None:
        nonlocal total_new, total_scraped
        total_scraped += len(batch)
        total_new += insert_sightings(batch)

    fetch_rare_birds(
        days_back_by_rarity=days_back_by_rarity,
        known_urls=known_urls,
        on_observations=_flush,
        progress_callback=progress_callback,
    )
    prune_old_sightings()
    return total_new, total_scraped


if __name__ == "__main__":
    print(f"[{datetime.now():%H:%M:%S}] Starting update…")

    def _log(pct: float, msg: str) -> None:
        bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
        print(f"\r  [{bar}] {msg:<55}", end="", flush=True)

    new, total = run_update(progress_callback=_log)
    print(f"\n[{datetime.now():%H:%M:%S}] Done — {new} new / {total} total scraped.")
