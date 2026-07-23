"""
Standalone updater — run periodically (e.g. every 30 min via cron):

    python updater.py                run a scrape (and the sweep when due)
    python updater.py --revalidate   force the revalidation sweep now

Per-rarity time windows:
  First run  : zeldzaam=7d, vrij zeldzaam=3d, vrij algemeen=1d
  Incremental: zeldzaam=2d, vrij zeldzaam=2d, vrij algemeen=1d
Entries older than 15 days are pruned after each run.

Once per REVALIDATE_EVERY_H a revalidation sweep re-fetches every
rare/very-rare sighting in the visible window: rows deleted on
waarneming.nl are removed, corrections (species, rarity, coords) are
synced, and photo URLs are backfilled for rows scraped before the
photo_url column existed.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from typing import Callable

from database import (
    init_db, insert_sightings, is_empty,
    get_all_urls, prune_old_sightings, prune_empty_subscribers,
    record_daily_stats, map_unmapped_locations, record_gemeente_daily,
    record_scrape_run,
    get_meta, set_meta, get_revalidation_candidates,
    delete_sighting, update_sighting,
)
from scraper import (
    fetch_rare_birds, check_observation, _make_authenticated_session,
)

REVALIDATE_EVERY_H = 24


def _revalidation_due() -> bool:
    last = get_meta("last_revalidation")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.utcnow() - last_dt >= timedelta(hours=REVALIDATE_EVERY_H)


def run_revalidation(
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[int, int, int]:
    """Re-check every candidate sighting against waarneming.nl.
    Returns (checked, updated, deleted)."""
    init_db()
    urls = get_revalidation_candidates()
    deleted = updated = 0
    if urls:
        session = _make_authenticated_session()
        for i, url in enumerate(urls):
            status, obs = check_observation(session, url)
            if status == "gone":
                delete_sighting(url)
                deleted += 1
            elif status == "ok" and update_sighting(obs):
                updated += 1
            if progress_callback:
                progress_callback(
                    (i + 1) / len(urls),
                    f"Hercontrole {i + 1}/{len(urls)}…",
                )
            time.sleep(0.35)
    set_meta("last_revalidation",
             datetime.utcnow().isoformat(timespec="seconds"))
    print(f"[reval] checked={len(urls)} updated={updated} "
          f"deleted={deleted}", flush=True)
    return len(urls), updated, deleted


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
    new_sightings: list[dict] = []

    def _flush(batch: list[dict]) -> None:
        nonlocal total_new, total_scraped
        total_scraped += len(batch)
        inserted = [s for s in batch if s["url"] not in known_urls]
        total_new += insert_sightings(batch)
        if not first_run:
            new_sightings.extend(inserted)

    fetch_rare_birds(
        days_back_by_rarity=days_back_by_rarity,
        known_urls=known_urls,
        on_observations=_flush,
        progress_callback=progress_callback,
    )
    prune_old_sightings()
    prune_empty_subscribers()
    map_unmapped_locations()
    record_daily_stats()
    record_gemeente_daily()

    if new_sightings:
        try:
            from notifications import send_push_notifications
            send_push_notifications(new_sightings)
        except Exception as exc:
            print(f"[fcm] notification error: {exc}", flush=True)

    record_scrape_run(total_new, total_scraped)

    if _revalidation_due():
        try:
            run_revalidation(progress_callback)
        except Exception as exc:
            print(f"[reval] error: {exc}", flush=True)

    return total_new, total_scraped


if __name__ == "__main__":
    def _log(pct: float, msg: str) -> None:
        bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
        print(f"\r  [{bar}] {msg:<55}", end="", flush=True)

    if "--revalidate" in sys.argv:
        print(f"[{datetime.now():%H:%M:%S}] Starting revalidation sweep…")
        checked, updated, deleted = run_revalidation(progress_callback=_log)
        print(f"\n[{datetime.now():%H:%M:%S}] Done — {checked} checked, "
              f"{updated} updated, {deleted} deleted.")
    else:
        print(f"[{datetime.now():%H:%M:%S}] Starting update…")
        new, total = run_update(progress_callback=_log)
        print(f"\n[{datetime.now():%H:%M:%S}] Done — {new} new / "
              f"{total} total scraped.")
