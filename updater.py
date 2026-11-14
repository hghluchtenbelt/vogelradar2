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

On the runs in between, a quick check re-fetches only the very-rare
rows (a few dozen), so a misidentified top-rarity entry disappears
within the hour instead of after a day.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from typing import Callable

from database import (
    init_db, insert_sightings, is_empty,
    get_pending_pushes, clear_pending_pushes,
    get_all_urls, prune_old_sightings, prune_subscribers,
    record_daily_stats, map_unmapped_locations, record_gemeente_daily,
    record_scrape_run,
    get_meta, set_meta, get_revalidation_candidates,
    delete_sighting, update_sighting, rename_bird_names,
)
from scraper import (
    fetch_rare_birds, check_observation, _make_authenticated_session,
)
from species import truncated_name_fixes

REVALIDATE_EVERY_H = 24

# A sighting that could not be announced (process restarted mid-scrape, FCM
# down) is retried on later runs until it is this old.
PUSH_MAX_AGE_H = 6


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
    min_rarity: int = 3,
) -> tuple[int, int, int]:
    """Re-check candidate sightings against waarneming.nl.
    min_rarity=3 is the daily full sweep (rare + very rare);
    min_rarity=4 is the hourly quick check (very rare only).
    Returns (checked, updated, deleted)."""
    init_db()
    urls = get_revalidation_candidates(min_rarity=min_rarity)
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
    now = datetime.utcnow().isoformat(timespec="seconds")
    result = json.dumps({
        "ts": now + "Z", "checked": len(urls),
        "updated": updated, "deleted": deleted,
    })
    if min_rarity <= 3:
        tag = "reval"
        set_meta("last_revalidation", now)
        set_meta("last_revalidation_result", result)
    else:
        tag = "reval-mini"
        set_meta("last_mini_revalidation_result", result)
    print(f"[{tag}] checked={len(urls)} updated={updated} "
          f"deleted={deleted}", flush=True)
    return len(urls), updated, deleted


def repair_truncated_names() -> int:
    """One-off: rows scraped before the name parser was fixed carry names
    cut at their first hyphen ("Jan" for Jan-van-gent). Rename them once,
    so the feed is right immediately instead of after those rows expire."""
    if get_meta("truncated_names_repaired"):
        return 0
    changed = rename_bird_names(truncated_name_fixes())
    set_meta("truncated_names_repaired", "1")
    print(f"[repair] {changed} truncated species names restored", flush=True)
    return changed


def send_pending_pushes() -> int:
    """Announce every stored sighting that has not been offered to the push
    loop yet. That includes rows left behind by a run that was interrupted
    (a deploy restarts the process mid-scrape): they are in the database,
    so the next scrape no longer sees them as new. Returns the number of
    sightings handled."""
    pending = get_pending_pushes(PUSH_MAX_AGE_H)
    if pending:
        try:
            from notifications import send_push_notifications
            send_push_notifications(pending)
        except Exception as exc:
            # Leave them pending: the next run retries, and the per-token
            # dedupe keeps anyone from being told twice.
            print(f"[fcm] notification error: {exc}", flush=True)
            clear_pending_pushes([], PUSH_MAX_AGE_H)
            return 0
    clear_pending_pushes([s["url"] for s in pending], PUSH_MAX_AGE_H)
    return len(pending)


def run_update(
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[int, int]:
    """Scrape and persist new sightings. Returns (new_count, total_scraped)."""
    init_db()
    repair_truncated_names()
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
        # On the very first run everything is "new"; announcing it all
        # would flood every subscriber.
        total_new += insert_sightings(batch, push_pending=not first_run)

    fetch_rare_birds(
        days_back_by_rarity=days_back_by_rarity,
        known_urls=known_urls,
        on_observations=_flush,
        progress_callback=progress_callback,
    )
    prune_old_sightings()
    prune_subscribers()
    map_unmapped_locations()
    record_daily_stats()
    record_gemeente_daily()

    send_pending_pushes()

    record_scrape_run(total_new, total_scraped)

    if _revalidation_due():
        try:
            run_revalidation(progress_callback)
        except Exception as exc:
            print(f"[reval] error: {exc}", flush=True)
    else:
        try:
            run_revalidation(progress_callback, min_rarity=4)
        except Exception as exc:
            print(f"[reval-mini] error: {exc}", flush=True)

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
