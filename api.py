"""FastAPI backend — serves /birds.json for the Vogelradar frontend."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

from typing import Annotated

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, StringConstraints, ValidationError

_rate_limit: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 3600
_RATE_MAX = 120
_RATE_MAX_KEYS = 20_000   # memory bound on the number of tracked addresses


def _client_ip(request: Request) -> str:
    """The visitor's address, for rate limiting only (never stored).

    Production runs behind Cloudflare and Traefik, so the socket peer is
    the proxy and every visitor would share one bucket. Cloudflare sets
    CF-Connecting-IP itself (a client cannot forge it through the edge)
    and the origin is only reachable through the tunnel. Without the
    header (local runs) the socket peer is used.
    """
    forwarded = request.headers.get("cf-connecting-ip", "").strip()
    if forwarded:
        return forwarded[:64]
    return request.client.host if request.client else "unknown"


def _allow_rate(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _rate_limit[ip] if now - t < _RATE_WINDOW]
    if len(recent) >= _RATE_MAX:
        _rate_limit[ip] = recent
        return False
    recent.append(now)
    _rate_limit[ip] = recent
    if len(_rate_limit) > _RATE_MAX_KEYS:
        # Drop addresses whose window has passed; if that is not enough
        # (a flood of distinct addresses), start over rather than grow.
        for key in [k for k, v in _rate_limit.items()
                    if not v or now - v[-1] >= _RATE_WINDOW]:
            del _rate_limit[key]
        if len(_rate_limit) > _RATE_MAX_KEYS:
            _rate_limit.clear()
    return True

from database import (
    get_sightings, get_latest_update, init_db,
    upsert_push_subscriber, delete_push_subscriber,
    get_daily_stats,
    get_gemeente_ranking, get_hotspot_ranking,
    get_gemeente_history, get_hotspots_in_gemeente, get_all_gemeentes,
    get_scrape_runs_since, get_meta,
)

# How often to re-scrape waarneming.nl in the background (seconds).
# Override with SCRAPE_INTERVAL_MIN (minutes); staging uses a longer
# interval so the extra instance stays polite towards waarneming.nl.
# 0 (or less) switches the in-process scraper off, e.g. for tests or when
# a scheduled task runs updater.py instead.
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL_MIN", "60")) * 60

app = FastAPI(title="Vogelradar", docs_url=None, redoc_url=None,
              openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
init_db()


# Forecast test (forecast/README.md): FORECAST_ENABLED=1 rebuilds the per-species
# layers from the database after every scrape; FORECAST_OUT_DIR points at a
# writable folder (a /data subfolder in the container). Needs numpy and Pillow.
FORECAST_ENABLED = os.environ.get("FORECAST_ENABLED", "0") == "1"
FORECAST_OUT_DIR = Path(os.environ.get("FORECAST_OUT_DIR") or (Path(__file__).parent / "forecast" / "out"))


def _run_forecast() -> None:
    import sys
    fdir = str(Path(__file__).parent / "forecast")
    if fdir not in sys.path:
        sys.path.insert(0, fdir)
    import pipeline  # noqa: E402 (forecast/pipeline.py)
    result = pipeline.run("db")
    print(f"[forecast] {result}", flush=True)


def _background_updater() -> None:
    """Run the updater once per SCRAPE_INTERVAL, forever, in a daemon thread."""
    from updater import run_update, send_pending_pushes
    try:
        # A restart in the middle of a scrape leaves stored sightings that
        # were never announced; send those now instead of an interval later.
        handled = send_pending_pushes()
        if handled:
            print(f"[updater] {handled} pending sightings announced", flush=True)
    except Exception as exc:
        print(f"[updater] pending push error: {exc}", flush=True)
    while True:
        time.sleep(SCRAPE_INTERVAL)
        try:
            new, total = run_update()
            print(f"[updater] {new} new / {total} scraped", flush=True)
        except Exception as exc:
            print(f"[updater] error: {exc}", flush=True)
        _invalidate_feed()
        if FORECAST_ENABLED:
            try:
                _run_forecast()
            except Exception as exc:
                print(f"[forecast] error: {exc}", flush=True)


# Start background updater when the server starts (daemon=True means it
# won't block a clean shutdown).
if SCRAPE_INTERVAL > 0:
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
        "img": row.get("photo_url") or "",
        "url": row["url"],
    }


# The feed is about 1.6 MB of JSON built from three queries, and it only
# changes when a scrape finishes or a sighting ages out of its window. It
# is built at most once per _FEED_TTL and served from memory in between.
_FEED_TTL = 60
_feed_cache: dict = {"t": 0.0, "body": b"", "etag": ""}
_feed_lock = threading.Lock()


def _invalidate_feed() -> None:
    _feed_cache["t"] = 0.0


def _feed() -> tuple[bytes, str]:
    with _feed_lock:
        now = time.time()
        if not _feed_cache["body"] or now - _feed_cache["t"] > _FEED_TTL:
            rows = get_sightings()
            last = get_latest_update()
            payload = {
                "lastUpdated": (last + "Z") if last and not last.endswith("Z")
                else (last or ""),
                "sightings": [_to_sighting(r) for r in rows],
            }
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
            _feed_cache.update(
                t=now, body=body,
                etag='"' + hashlib.sha256(body).hexdigest()[:20] + '"',
            )
        return _feed_cache["body"], _feed_cache["etag"]


def _etag_matches(header: str, etag: str) -> bool:
    # A proxy that compresses the response turns the tag into a weak one.
    tags = {t.strip().removeprefix("W/") for t in header.split(",")}
    return etag in tags or "*" in tags


@app.get("/birds.json")
def birds_json(request: Request):
    body, etag = _feed()
    headers = {"Cache-Control": f"public, max-age={_FEED_TTL}", "ETag": etag}
    if _etag_matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@app.get("/healthz")
def healthz():
    """Container healthcheck: proves the process answers and the database
    opens, without building the feed every 30 seconds."""
    get_meta("last_revalidation")
    return {"ok": True}


@app.get("/species_data.js")
def species_data():
    return FileResponse(
        _HERE / "species_data.js", media_type="application/javascript"
    )


@app.get("/privacy")
def privacy():
    return FileResponse(_HERE / "privacy.html", media_type="text/html")


@app.get("/icon.png")
@app.get("/favicon.ico")
def icon():
    return FileResponse(_HERE / "icon.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


_places_body: bytes = b""


@app.get("/places.json")
def places_json():
    """Municipalities with a centre point, for the manual location picker."""
    global _places_body
    if not _places_body:
        from gemeente import places
        _places_body = json.dumps(
            {"places": [list(p) for p in places()]},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    return Response(_places_body, media_type="application/json",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest")
def manifest():
    return Response(
        json.dumps({
            "name": "Vogelradar",
            "short_name": "Vogelradar",
            "description": "Zeldzame vogels bij jou in de buurt",
            "lang": "nl",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0d9488",
            "theme_color": "#0d9488",
            "icons": [{"src": "/icon.png", "sizes": "512x512",
                       "type": "image/png", "purpose": "any"}],
        }, ensure_ascii=False),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# Limits for POST /register-token. The wish list holds species names plus
# the two "all rare" keys, so 2000 entries is far above anything the app can
# send; world bounds (not just the Netherlands) because users travel.
_MAX_REGISTER_BODY = 128 * 1024
_WishName = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class PushSubscriber(BaseModel):
    token: str = Field(min_length=20, max_length=4096)
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    wishlist: list[_WishName] = Field(default_factory=list, max_length=2000)
    max_dist: int = Field(default=50, ge=0, le=20000)


async def _read_body(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything larger than `limit` bytes
    (also when the client lies about, or omits, Content-Length)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail="Request body too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/register-token")
async def register_token(request: Request):
    if not _allow_rate(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests")
    raw = await _read_body(request, _MAX_REGISTER_BODY)
    try:
        body = PushSubscriber.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False,
                              include_input=False),
        ) from None
    if not body.wishlist:
        # Nothing to notify about, so nothing is kept: no token, no
        # location. Released app versions post at startup before any wish
        # is chosen, and post an empty list when the last wish is removed.
        await run_in_threadpool(delete_push_subscriber, body.token)
        return {"ok": True}
    await run_in_threadpool(
        upsert_push_subscriber,
        body.token, body.lat, body.lng,
        json.dumps(body.wishlist), body.max_dist,
    )
    return {"ok": True}


@app.delete("/register-token/{token}")
def unregister_token(token: str, request: Request):
    if not _allow_rate(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests")
    delete_push_subscriber(token)
    return {"ok": True}


_STATS_TTL = 300  # cache computed stats for 5 min (spam-proof; data is hourly)
_stats_cache: dict = {"t": 0.0, "data": None}
_gem_cache: dict[str, tuple[float, dict]] = {}


@app.get("/stats.json")
def stats_json():
    now = time.time()
    if _stats_cache["data"] is None or now - _stats_cache["t"] > _STATS_TTL:
        _stats_cache["data"] = {
            "days": get_daily_stats(4000),          # full history; sliced client-side
            "gemeentes": get_gemeente_ranking(7, 10),
            "hotspots": get_hotspot_ranking(7, 10),
            "gemeente_list": get_all_gemeentes(),
        }
        _stats_cache["t"] = now
    return _stats_cache["data"]


@app.get("/stats/gemeente.json")
def gemeente_detail(name: str):
    now = time.time()
    hit = _gem_cache.get(name)
    if hit and now - hit[0] <= _STATS_TTL:
        return hit[1]
    data = {
        "name": name,
        "history": get_gemeente_history(name),
        "hotspots": get_hotspots_in_gemeente(name, 7, 200),
    }
    if len(_gem_cache) > 200:
        _gem_cache.clear()
    _gem_cache[name] = (now, data)
    return data


@app.get("/scrape-stats.json")
def scrape_stats(hours: int = 12):
    """Aggregate scrape activity for the status bot (no PII)."""
    hours = max(1, min(hours, 168))
    runs = get_scrape_runs_since(hours)
    reval = get_meta("last_revalidation_result")
    mini = get_meta("last_mini_revalidation_result")
    return {
        "hours": hours,
        "total_new": sum(r["new_count"] for r in runs),
        "runs": runs,  # [{ts (UTC ISO), new_count, total_scraped}]
        # last revalidation sweep: {ts, checked, updated, deleted} | null
        "revalidation": json.loads(reval) if reval else None,
        # last hourly very-rare quick check, same shape | null
        "mini_revalidation": json.loads(mini) if mini else None,
    }


@app.get("/stats")
def stats_page():
    return FileResponse(_HERE / "statistieken.html", media_type="text/html")


_FORECAST_DIR = _HERE / "forecast"
_FORECAST_FILE_RE = re.compile(r"[a-z0-9-]+\.(png|json)")


@app.get("/voorspelling")
def forecast_page():
    """Test tab: per-species "activity now" map (see forecast/README.md)."""
    return FileResponse(_FORECAST_DIR / "voorspelling.html", media_type="text/html")


@app.get("/forecast/{name}")
def forecast_file(name: str):
    """Precomputed forecast layers (PNG overlays and JSON meta) from forecast/out."""
    if not _FORECAST_FILE_RE.fullmatch(name):
        raise HTTPException(status_code=404)
    path = FORECAST_OUT_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=300"})


@app.get("/")
def index():
    # "no-cache": always revalidate, so a deploy reaches visitors at once.
    return FileResponse(_HERE / "vogelradar.html",
                        headers={"Cache-Control": "no-cache"})
