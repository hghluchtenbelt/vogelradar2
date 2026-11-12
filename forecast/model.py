"""Rare-bird "activity now" forecast: ranked 1 km grid per species.

For one species and one as-of date the model ranks Dutch 1 km cells by
expected activity on the next day:

  kernel(cell)  = sum over sites of  weight(site) * [ p_stay * Gauss(d, sigma) + (1 - p_stay) * Exp(d', scale) ]
  habitat(cell) = sum over classes of w_class * fraction_class * coast term * onshore-wind term
  score(cell)   = (kernel / max kernel)^gamma * (0.15 + 0.85 * habitat)

* sites: reports of the species within 2 km form one site (a staked-out bird
  reported many times counts once);
* weight(site) grows with reports^0.3, days present^0.5, a photo and the
  number of birds, and decays with the age of the last report (site_weight);
* d' is the distance from the site shifted downwind by the night wind for
  guilds that drift (migrant passerines, raptors);
* the onshore-wind term boosts coast cells facing the day wind (seabirds).

Parameters come from the species' method group (guilds.py) with overrides
for a few well-studied species (SPECIES_OVERRIDES); habitat weights are a
50/50 blend of the group's hand weights and weights learned from the
species' own sites when there are at least 10.

Run:  .venv/bin/python forecast/model.py --species all   (every rare or very
rare species in the archived data) or --species <slug>.
Needs numpy, scipy and Pillow (offline precompute only).
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np

import stay as staymod
import wind as windmod
from guilds import GUILDS, canonical_name, guild_for, norm_name, slugify
from species_list import load_universe

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")                                   # grid file and local snapshots (repo)
OUT_DIR = os.environ.get("FORECAST_OUT_DIR") or os.path.join(HERE, "out")  # generated layers (volume in the container)

RARITY_FROM_FEED = {"common": 1, "uncommon": 2, "rare": 3, "very_rare": 4}
WORLDCOVER_NAMES = {10: "tree", 20: "shrub", 30: "grass", 40: "cropland", 50: "built", 60: "bare",
                    70: "snow", 80: "water", 90: "wetland", 95: "mangrove", 100: "moss"}
WORLDCOVER_NAMES[40] = "crop"
ALL_FEATURES = frozenset({"days", "photo", "count"})
MIN_RARITY = 3

# Species-level overrides on top of the guild defaults (profile text is UI Dutch).
SPECIES_OVERRIDES = {
    "notenkraker": {
        "profile": "Invasiesoort uit Siberië; blijft hangen waar voedsel is (hazelaars, dennen, parken, tuinen) en verplaatst zich daarna tientallen km.",
        "p_stay": 0.8, "sigma_stay_km": 4.0, "scale_move_km": 30.0, "decay_days": 5.0,
        "habitat": {"tree": 1.0, "shrub": 0.7, "built": 0.55, "grass": 0.25, "crop": 0.1, "bare": 0.2, "wetland": 0.1}},
    "klapekster": {
        "profile": "Wintergast die een territorium bezet op heide, kapvlaktes en hoogveen met losse bomen; blijft vaak de hele winter.",
        "habitat": {"shrub": 1.0, "grass": 0.6, "tree": 0.5, "bare": 0.5, "wetland": 0.6, "crop": 0.1, "built": 0.05}},
    "bladkoning": {
        "profile": "Siberische doortrekker in oktober; korte stops in kustbosjes, struweel en dorpstuinen, daarna weer weg.",
        "coast_pref": 0.7, "habitat": {"tree": 1.0, "shrub": 1.0, "built": 0.5, "grass": 0.1, "wetland": 0.2}},
    "ijsgors": {
        "profile": "Doortrekker en wintergast van open kustland: kwelders, zeedijken, akkers en duingrasland.",
        "coast_pref": 0.8, "habitat": {"grass": 1.0, "crop": 0.7, "bare": 0.6, "wetland": 0.6, "shrub": 0.2}},
    "zwarte-ibis": {
        "profile": "Zwerver uit Zuid-Europa, vaak in groepjes; foerageert in natte graslanden en moeras en verplaatst zich tientallen km.",
        "p_stay": 0.7, "scale_move_km": 40.0, "decay_days": 5.0, "coast_pref": 0.0,
        "habitat": {"wetland": 1.0, "grass": 0.7, "water": 0.6, "crop": 0.2}},
    "grote-pieper": {
        "profile": "Doortrekker uit het oosten; korte stops op kale akkers, kwelders, duingrasland en opspuitterreinen, vooral bij de kust.",
        "p_stay": 0.5, "scale_move_km": 50.0, "decay_days": 2.0, "coast_pref": 0.5,
        "habitat": {"grass": 1.0, "crop": 0.6, "bare": 0.7, "wetland": 0.4, "shrub": 0.1}},
    "siberische-tjiftjaf": {
        "profile": "Late doortrekker en overwinteraar in struweel, tuinen en bij waterzuiveringen; kan weken blijven.",
        "p_stay": 0.7, "decay_days": 4.0, "coast_pref": 0.0,
        "habitat": {"shrub": 1.0, "tree": 0.8, "built": 0.6, "wetland": 0.5, "grass": 0.1}},
    "kuifduiker": {
        "profile": "Wintergast op open water: IJsselmeer, Grevelingen, plassen en de kust; honkvast zolang het water open blijft.",
        "coast_pref": 0.4, "habitat": {"water": 1.0, "wetland": 0.3}},
    "steppekiekendief": {
        "profile": "Roofvogel op doortrek, zeer mobiel; jaagt boven open akkers en graslanden en is de volgende dag vaak ver weg."},
    "roodhalsgans": {
        "profile": "Wintergast tussen brand- en rotganzen op kustgrasland; trekt mee met de groep en blijft vaak weken in dezelfde polder.",
        "coast_pref": 0.7},
}


def species_config(label: str, names: list[str], family: str | None = None, sci: str | None = None) -> dict:
    """Guild defaults plus species overrides."""
    g = guild_for(label, family, sci)
    gp = GUILDS[g]
    cfg = {"label": label, "names": names, "guild": g, "guild_label": gp["label"], "profile": "",
           "window_days": 7, "site_km": 2.0, "kernel_gamma": 0.3, "coast_scale_km": gp.get("coast_scale_km", 20.0),
           "p_stay": gp["p_stay"], "sigma_stay_km": gp["sigma_stay_km"], "scale_move_km": gp["scale_move_km"],
           "decay_days": gp["decay_days"], "coast_pref": gp["coast_pref"], "wind": dict(gp["wind"]),
           "habitat": dict(gp["habitat"]), "combine": "mult", "c_move": 0.7, "anchor_km": 3.0}
    cfg.update(SPECIES_OVERRIDES.get(slugify(label), {}))
    cfg["sci"] = sci or ""
    cfg["name_keys"] = {norm_name(n) for n in names}
    return cfg


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------

def _norm_feed(s: dict) -> dict | None:
    try:
        return {"url": s.get("url") or s.get("id"), "nl": s["nl"], "lat": float(s["lat"]),
                "lon": float(s["lng"]), "date": s["date"], "time": s.get("time") or "",
                "count": int(s.get("count") or 1), "rarity": RARITY_FROM_FEED.get(s.get("rarity"), 0),
                "loc": s.get("loc") or "", "photo": bool(s.get("photo") or s.get("img")), "key": norm_name(s["nl"])}
    except (KeyError, TypeError, ValueError):
        return None


def _norm_db(s: dict) -> dict | None:
    try:
        return {"url": s["url"], "nl": s["bird_name"], "lat": float(s["latitude"]),
                "lon": float(s["longitude"]), "date": s["date"], "time": s.get("obs_time") or "",
                "count": int(s.get("count") or 1), "rarity": int(s.get("rarity") or 0),
                "loc": s.get("location") or "", "photo": bool(s.get("photo") or s.get("photo_url")),
                "key": norm_name(s["bird_name"])}
    except (KeyError, TypeError, ValueError):
        return None


def load_sightings() -> list[dict]:
    """All known reports from every archived feed snapshot and database export,
    deduplicated by observation URL (the latest file wins)."""
    by_url: dict[str, dict] = {}
    files = sorted(glob.glob(os.path.join(DATA_DIR, "snapshots", "*.json.gz")))
    files += sorted(glob.glob(os.path.join(DATA_DIR, "prod-export-*.json.gz")))
    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        rows = d["sightings"] if isinstance(d, dict) else d
        for s in rows:
            r = _norm_db(s) if "bird_name" in s else _norm_feed(s)
            if r and r["url"]:
                by_url[r["url"]] = r
    return list(by_url.values())


def species_registry(reports: list[dict], min_rarity: int = MIN_RARITY) -> dict[str, dict]:
    """Every rare or very rare species of the waarneming.nl export (species_list.py),
    plus any species the reports themselves mark rare that the export does not
    (source "feed"), keyed by slug."""
    reg: dict[str, dict] = {}
    key_to_slug: dict[str, str] = {}
    for label, u in load_universe().items():
        slug = slugify(label)
        cfg = species_config(label, sorted(u["names"]), family=u["family"], sci=u["sci"])
        cfg.update(rarity_class=u["rarity_class"], source="export", waarneming_id=u["waarneming_id"])
        reg[slug] = cfg
        for n in u["names"]:
            key_to_slug[norm_name(n)] = slug
    extra: dict[str, set] = defaultdict(set)
    for r in reports:
        if r["rarity"] < min_rarity:
            continue
        c = canonical_name(r["nl"])
        if not c or r["key"] in key_to_slug or norm_name(c) in key_to_slug:
            continue
        c = re.sub(r"\s*\(?ssp\.?\s+[^)]*\)?\s*$", "", c, flags=re.I).strip() or c   # extras: subspecies join the species
        extra[c].add(r["nl"])
    for label, names in extra.items():
        slug = slugify(label)
        if slug in reg:
            reg[slug]["names"] = sorted(set(reg[slug]["names"]) | names)
            reg[slug]["name_keys"] |= {norm_name(n) for n in names}
            continue
        cfg = species_config(label, sorted(names))
        cfg.update(rarity_class="zeldzaam volgens de meldingen", source="feed", waarneming_id="")
        reg[slug] = cfg
    return reg


def discover_species(reports: list[dict], min_rarity: int = MIN_RARITY) -> dict[str, dict]:
    """Registry entries that have at least one report at the given rarity."""
    reg = species_registry(reports, min_rarity)
    seen = {r["key"] for r in reports if r["rarity"] >= min_rarity}
    return {slug: cfg for slug, cfg in reg.items() if cfg["name_keys"] & seen}


def nl_mask(ny: int, nx: int, lat_edges: np.ndarray, lon_edges: np.ndarray) -> np.ndarray:
    """Rasterise the Dutch municipalities (gemeentes.geojson, WGS84) onto the grid,
    so rankings and evaluation cover the Netherlands only."""
    from PIL import Image, ImageDraw
    path = os.path.join(os.path.dirname(HERE), "gemeentes.geojson")
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    dlat = float(lat_edges[0] - lat_edges[1]); dlon = float(lon_edges[1] - lon_edges[0])
    img = Image.new("1", (nx, ny), 0)
    draw = ImageDraw.Draw(img)
    for feat in gj["features"]:
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            pts = [((lon - float(lon_edges[0])) / dlon, (float(lat_edges[0]) - lat) / dlat) for lon, lat in poly[0]]
            if len(pts) >= 3:
                draw.polygon(pts, fill=1)
    return np.array(img, dtype=bool)


def gemeente_raster(ny: int, nx: int, lat_edges: np.ndarray, lon_edges: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Municipality index per cell (0 = none) from gemeentes.geojson, for area summaries."""
    from PIL import Image, ImageDraw
    path = os.path.join(os.path.dirname(HERE), "gemeentes.geojson")
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    dlat = float(lat_edges[0] - lat_edges[1]); dlon = float(lon_edges[1] - lon_edges[0])
    img = Image.new("I", (nx, ny), 0)
    draw = ImageDraw.Draw(img)
    names = []
    for feat in gj["features"]:
        names.append(feat["properties"].get("statnaam") or f"gebied {len(names) + 1}")
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            pts = [((lon - float(lon_edges[0])) / dlon, (float(lat_edges[0]) - lat) / dlat) for lon, lat in poly[0]]
            if len(pts) >= 3:
                draw.polygon(pts, fill=len(names))
    return np.array(img, dtype=np.int32), names


def area_chances(lam: np.ndarray, grid: dict, min_p: float = 0.005) -> dict[str, dict]:
    """Chance of at least one report per municipality: 1 - exp(-sum of lambda over its cells)."""
    sums = np.bincount(grid["gem"].ravel(), weights=lam.ravel(), minlength=len(grid["gem_names"]) + 1)
    out = {}
    for i, name in enumerate(grid["gem_names"], start=1):
        p = 1 - math.exp(-float(sums[i]))
        if p >= min_p:
            out[name] = {"p": round(p, 3), "expected": round(float(sums[i]), 3)}
    return out


def load_grid() -> dict:
    d = np.load(os.path.join(DATA_DIR, "landcover_1km.npz"))
    frac = d["frac"].astype(np.float32) / 100.0           # (k, ny, nx)
    classes = [WORLDCOVER_NAMES[int(c)] for c in d["classes"]]
    lat_edges, lon_edges = d["lat_edges"], d["lon_edges"]   # lat descending, lon ascending
    lat_c = (lat_edges[:-1] + lat_edges[1:]) / 2
    lon_c = (lon_edges[:-1] + lon_edges[1:]) / 2
    ny, nx = len(lat_c), len(lon_c)
    nl = nl_mask(ny, nx, lat_edges, lon_edges)
    gem, gem_names = gemeente_raster(ny, nx, lat_edges, lon_edges)
    land = (frac.sum(axis=0) >= 0.2) & nl                   # Dutch cells with >= 20 percent classified land
    if "coast_km" in d.files:
        coast_km, n_e, n_n = d["coast_km"].astype(np.float64), d["coast_n_e"].astype(np.float64), d["coast_n_n"].astype(np.float64)
    else:  # older grid file: derive on the fly (needs scipy)
        from scipy.ndimage import distance_transform_edt, gaussian_filter
        sea = frac.sum(axis=0) < 0.2                        # open sea and tidal flats are unclassified
        coast_km = distance_transform_edt(~sea, sampling=(1.11, 1.03))
        sm = gaussian_filter(sea.astype(np.float64), sigma=3)
        g_row, g_col = np.gradient(sm)
        n_e, n_n = g_col, -g_row
        mag = np.hypot(n_e, n_n)
        n_e = np.where(mag > 1e-6, n_e / np.maximum(mag, 1e-9), 0.0)
        n_n = np.where(mag > 1e-6, n_n / np.maximum(mag, 1e-9), 0.0)
    return {"frac": frac, "classes": classes, "lat_edges": lat_edges, "lon_edges": lon_edges,
            "lat_c": lat_c, "lon_c": lon_c, "land": land, "nl": nl, "gem": gem, "gem_names": gem_names,
            "ny": ny, "nx": nx, "coast_km": coast_km,
            "coast_n_e": n_e, "coast_n_n": n_n,
            "dlat": float(lat_edges[0] - lat_edges[1]), "dlon": float(lon_edges[1] - lon_edges[0])}


def cell_index(grid: dict, lat: float, lon: float) -> tuple[int, int] | None:
    iy = int((grid["lat_edges"][0] - lat) / grid["dlat"])
    ix = int((lon - grid["lon_edges"][0]) / grid["dlon"])
    if 0 <= iy < grid["ny"] and 0 <= ix < grid["nx"]:
        return iy, ix
    return None


# ----------------------------------------------------------------------------
# Geometry and sites
# ----------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2) - np.radians(lon1)
    h = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def species_reports(reports: list[dict], cfg: dict, as_of: date, window_days: int | None = None) -> list[dict]:
    keys = cfg["name_keys"]
    w = cfg["window_days"] if window_days is None else window_days
    lo = as_of - timedelta(days=w - 1)
    return [r for r in reports if r["key"] in keys and lo <= parse_date(r["date"]) <= as_of]


def cluster_sites(reports: list[dict], site_km: float) -> list[dict]:
    """Greedy clustering, newest report first: a report joins the first site
    within site_km, otherwise starts a new one."""
    reports = sorted(reports, key=lambda r: (r["date"], r["time"]), reverse=True)
    sites: list[dict] = []
    for r in reports:
        d = parse_date(r["date"])
        for s in sites:
            if haversine_km(r["lat"], r["lon"], s["lat"], s["lon"]) <= site_km:
                s["members"].append(r)
                s["first_date"] = min(s["first_date"], d)
                s["last_date"] = max(s["last_date"], d)
                s["dates"].add(d)
                s["subspecies"].add(r["nl"])
                break
        else:
            sites.append({"lat": r["lat"], "lon": r["lon"], "members": [r], "first_date": d,
                          "last_date": d, "dates": {d}, "subspecies": {r["nl"]}})
    for s in sites:
        s["lat"] = float(np.mean([m["lat"] for m in s["members"]]))
        s["lon"] = float(np.mean([m["lon"] for m in s["members"]]))
        s["n_reports"] = len(s["members"])
        s["n_days"] = len(s["dates"])
        s["max_count"] = max(m["count"] for m in s["members"])
        s["n_photo"] = sum(1 for m in s["members"] if m.get("photo"))
        s["loc"] = s["members"][0]["loc"]
    return sites


def site_weight(s: dict, cfg: dict, as_of: date, features: frozenset = ALL_FEATURES) -> tuple[float, float]:
    """Weight and stay share of one site.

    weight = n_reports^0.3 * n_days^0.5 * photo factor (1.25 / 0.8)
           * (1 + 0.1 ln max_count) * exp(-age / decay_days)
    stay share rises with the days already stayed: p = 1 - (1 - p_stay) * 0.7^(n_days - 1)
    """
    age = (as_of - s["last_date"]).days
    w = s["n_reports"] ** 0.3 * math.exp(-age / cfg["decay_days"])
    p = cfg["p_stay"]
    if "days" in features:
        w *= s["n_days"] ** 0.5
        p = 1 - (1 - p) * 0.7 ** (s["n_days"] - 1)
    if "photo" in features:
        w *= 1.25 if s.get("n_photo", 0) > 0 else 0.8
    if "count" in features:
        w *= 1 + 0.1 * math.log(max(1, s["max_count"]))
    return w, p


# ----------------------------------------------------------------------------
# Wind
# ----------------------------------------------------------------------------

KM_PER_MS_NIGHT = 7.2     # 0.25 of the 10 m wind over 8 hours, km per m/s
MAX_DRIFT_KM = 60.0


def wind_fields(grid: dict, wdata: dict | None, target_day: date) -> dict:
    """Night wind (drift) and day wind (onshore) on the grid for the target day."""
    out = {}
    if not wdata:
        return out
    nw = windmod.night_wind(wdata, target_day)
    dw = windmod.day_wind(wdata, target_day)
    if nw:
        out["night"] = windmod.on_grid(grid, *nw); out["night_summary"] = windmod.summary(*nw)
    if dw:
        out["day"] = windmod.on_grid(grid, *dw); out["day_summary"] = windmod.summary(*dw)
    return out


def kernel_field(grid: dict, sites: list[dict], cfg: dict, as_of: date, features: frozenset = ALL_FEATURES,
                 winds: dict | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Weighted two-state kernels, returned as (stayed, moved) fields; the
    "moved" state is shifted downwind for drifting guilds when night wind is
    available."""
    lat2, lon2 = np.meshgrid(grid["lat_c"], grid["lon_c"], indexing="ij")
    stay_f = np.zeros((grid["ny"], grid["nx"]), dtype=np.float64)
    move_f = np.zeros_like(stay_f)
    sig, sc = cfg["sigma_stay_km"], cfg["scale_move_km"]
    k_drift = cfg.get("wind", {}).get("drift", 0.0)
    night = winds.get("night") if winds else None
    for s in sites:
        w, p = site_weight(s, cfg, as_of, features)
        d = haversine_km(s["lat"], s["lon"], lat2, lon2)
        stay = np.exp(-d ** 2 / (2 * sig ** 2)) / (2 * math.pi * sig ** 2)
        mlat, mlon = s["lat"], s["lon"]
        if k_drift and night is not None:
            c = cell_index(grid, s["lat"], s["lon"])
            if c:
                u, v = float(night[0][c]), float(night[1][c])
                dx, dy = u * KM_PER_MS_NIGHT * k_drift, v * KM_PER_MS_NIGHT * k_drift
                m = math.hypot(dx, dy)
                if m > MAX_DRIFT_KM:
                    dx, dy = dx * MAX_DRIFT_KM / m, dy * MAX_DRIFT_KM / m
                mlat = s["lat"] + dy / 111.0
                mlon = s["lon"] + dx / (111.0 * math.cos(math.radians(s["lat"])))
        dm = d if (mlat == s["lat"] and mlon == s["lon"]) else haversine_km(mlat, mlon, lat2, lon2)
        move = np.exp(-dm / sc) / (2 * math.pi * sc ** 2)
        stay_f += w * p * stay
        move_f += w * (1 - p) * move
    return stay_f, move_f


def habitat_field(grid: dict, weights: dict[str, float], coast_pref: float = 0.0, coast_scale_km: float = 20.0,
                  onshore_k: float = 0.0, day_wind=None) -> np.ndarray:
    """Weighted land-cover fractions, tilted towards the coast (coast_pref) and,
    for seabird-like guilds, towards coast cells facing the day wind."""
    h = np.zeros((grid["ny"], grid["nx"]), dtype=np.float64)
    for i, name in enumerate(grid["classes"]):
        h += weights.get(name, 0.0) * grid["frac"][i]
    if coast_pref > 0:
        h *= (1 - coast_pref) + coast_pref * np.exp(-grid["coast_km"] / coast_scale_km)
    if onshore_k > 0 and day_wind is not None:
        u, v = day_wind
        onshore = np.clip(-(u * grid["coast_n_e"] + v * grid["coast_n_n"]), 0, 12)   # m/s blowing landward
        h *= 1 + onshore_k * (onshore / 8.0) * np.exp(-grid["coast_km"] / 10.0)
    m = h[grid["land"]].max() if grid["land"].any() else 1.0
    return h / m if m > 0 else h


def learned_habitat_weights(grid: dict, sites: list[dict]) -> dict[str, float]:
    """Class-ratio weights: mean class fraction at the species' sites divided by
    the mean over all land cells (a cheap presence-only habitat preference)."""
    cells = [cell_index(grid, s["lat"], s["lon"]) for s in sites]
    cells = [c for c in cells if c is not None]
    if len(cells) < 5:
        return {}
    iy = np.array([c[0] for c in cells]); ix = np.array([c[1] for c in cells])
    at_sites = grid["frac"][:, iy, ix].mean(axis=1)
    background = grid["frac"][:, grid["land"]].mean(axis=1)
    ratio = np.where(background > 0.005, at_sites / np.maximum(background, 1e-6), 0.0)
    ratio = np.clip(ratio, 0, None)
    if ratio.max() <= 0:
        return {}
    ratio = ratio / ratio.max()
    return {name: float(ratio[i]) for i, name in enumerate(grid["classes"])}


PROB_THRESHOLDS = [0.30, 0.10, 0.03, 0.01]      # legend classes, the same for every species
# Re-reports land close to the site (median 0.18 km, 75th percentile 0.95 km): a
# narrow core plus a wider tail.
SPREAD_CORE_KM, SPREAD_TAIL_KM, SPREAD_CORE_SHARE = 0.25, 1.2, 0.6


def probability_field(grid: dict, sites: list[dict], cfg: dict, as_of: date, move_f: np.ndarray, hab: np.ndarray,
                      stay_model: dict | None, n_new: float, gamma: float) -> tuple[np.ndarray, np.ndarray, list[float], np.ndarray]:
    """Expected number of reports per cell tomorrow, and the chance of at least one:

        lambda(cell) = sum_sites p_stay(site) * spread(cell | site) + n_new * density_new(cell)
        P(cell)      = 1 - exp(-lambda)

    p_stay comes from the fitted stay model (stay.py); spread is a 0.6 km
    Gaussian normalised over the cells within 5 km; density_new is the
    "moved" kernel times habitat (coast and wind included), normalised to one
    over Dutch land cells."""
    land = grid["land"]
    lat2, lon2 = np.meshgrid(grid["lat_c"], grid["lon_c"], indexing="ij")
    lam = np.zeros((grid["ny"], grid["nx"]), dtype=np.float64)
    p_sites = []
    for s in sites:
        p = staymod.predict(stay_model, s, cfg, as_of)
        p_sites.append(p)
        d = haversine_km(s["lat"], s["lon"], lat2, lon2)
        core = np.exp(-d ** 2 / (2 * SPREAD_CORE_KM ** 2)); core /= max(core[land & (d <= 5.0)].sum(), 1e-12)
        tail = np.exp(-d ** 2 / (2 * SPREAD_TAIL_KM ** 2)); tail /= max(tail[land & (d <= 5.0)].sum(), 1e-12)
        spread = np.where((d <= 5.0) & land, SPREAD_CORE_SHARE * core + (1 - SPREAD_CORE_SHARE) * tail, 0.0)
        lam += p * spread
    lam_new = np.zeros_like(lam)
    if n_new > 0 and sites:
        dens = combine(move_f, hab, grid, gamma)
        dens[~land] = 0.0
        tot = dens.sum()
        if tot > 0:
            lam_new = n_new * dens / tot
            lam += lam_new
    prob = 1 - np.exp(-lam)
    prob[~land] = 0.0
    return lam, prob, p_sites, lam_new


def prob_classes(prob: np.ndarray, land: np.ndarray) -> tuple[np.ndarray, dict]:
    """Absolute classes: 4 = chance >= 30 percent, 3 = >= 10, 2 = >= 3, 1 = >= 1, 0 = below 1 or sea."""
    cls = np.zeros(prob.shape, dtype=np.uint8)
    for lvl, thr in zip((1, 2, 3, 4), reversed(PROB_THRESHOLDS)):
        cls[(prob >= thr) & land] = lvl
    names = ("p30", "p10", "p3", "p1")
    counts = {n: int((cls == lvl).sum()) for lvl, n in zip((4, 3, 2, 1), names)}
    counts["land_cells"] = int(land.sum())
    return cls, counts


def rank_classes(score: np.ndarray, land: np.ndarray) -> tuple[np.ndarray, dict]:
    """Percentile classes over land cells: 4 = top 1 percent, 3 = top 5, 2 = top 10,
    1 = top 25, 0 = rest or sea."""
    cls = np.zeros(score.shape, dtype=np.uint8)
    vals = score[land]
    n = vals.size
    order = np.argsort(-vals)
    ranks = np.empty(n, dtype=np.int64); ranks[order] = np.arange(n)
    land_idx = np.flatnonzero(land)
    thresholds = {"top1": 0.01, "top5": 0.05, "top10": 0.10, "top25": 0.25}
    for level, (name, q) in zip((4, 3, 2, 1), thresholds.items()):
        sel = ranks < max(1, int(round(q * n)))
        c = cls.ravel()
        target = c[land_idx[sel]]
        target[target == 0] = level
        c[land_idx[sel]] = target
    counts = {name: int((cls == lvl).sum()) for lvl, name in zip((4, 3, 2, 1), thresholds)}
    counts["land_cells"] = int(n)
    return cls, counts


def top_fraction_mask(score: np.ndarray, land: np.ndarray, q: float) -> np.ndarray:
    vals = score[land]
    k = max(1, int(round(q * vals.size)))
    thr = np.partition(vals, -k)[-k]
    return land & (score >= thr)


def _norm(field: np.ndarray, land: np.ndarray) -> np.ndarray:
    m = field[land].max() if land.any() and field[land].max() > 0 else 1.0
    return np.clip(field / m, 1e-12, None)


def combine(kernel: np.ndarray, hab: np.ndarray, grid: dict, gamma: float) -> np.ndarray:
    """Multiplicative form (version 2): (kernel / max)^gamma * (0.15 + 0.85 * habitat)."""
    land = grid["land"]
    score = _norm(kernel, land) ** gamma * (0.15 + 0.85 * hab)
    score[~land] = 0.0
    return score


def combine_additive(stay: np.ndarray, move: np.ndarray, hab: np.ndarray, grid: dict, gamma: float,
                     c_move: float = 0.7) -> np.ndarray:
    """Version 3: habitat steers only the "moved" part. A known site keeps its full
    "stayed" mass whatever its land cover (a bird reported 21 times in a village
    is in the village).

        score = (stay / max stay)^gamma + c_move * ((move * habitat) / max)^gamma
    """
    land = grid["land"]
    score = _norm(stay, land) ** gamma + c_move * _norm(move * (0.15 + 0.85 * hab), land) ** gamma
    score[~land] = 0.0
    return score


# ----------------------------------------------------------------------------
# Forecast
# ----------------------------------------------------------------------------

def anchor_sites(score: np.ndarray, grid: dict, sites: list[dict], as_of: date, radius_km: float = 3.0,
                 max_age_days: int = 2) -> np.ndarray:
    """A site reported in the last days is never ranked below its own surroundings:
    its cell gets at least the highest score within radius_km. Habitat may still
    rank a forest next to a village high, but the village where the bird was
    actually seen stays on top."""
    out = score.copy()
    lat2, lon2 = np.meshgrid(grid["lat_c"], grid["lon_c"], indexing="ij")
    for s in sites:
        if (as_of - s["last_date"]).days > max_age_days:
            continue
        c = cell_index(grid, s["lat"], s["lon"])
        if c is None or not grid["land"][c]:
            continue
        near = (haversine_km(s["lat"], s["lon"], lat2, lon2) <= radius_km) & grid["land"]
        out[c] = max(out[c], float(score[near].max()))
    return out


def build_forecast(reports: list[dict], grid: dict, cfg: dict, as_of: date, habitat: str = "blend",
                   gamma: float | None = None, features: frozenset = ALL_FEATURES,
                   wdata: dict | None = None, use_wind: bool = True, anchor: bool = True,
                   stay_model: dict | None = None) -> dict:
    sp = species_reports(reports, cfg, as_of)
    sites = cluster_sites(sp, cfg["site_km"])
    winds = wind_fields(grid, wdata, as_of + timedelta(days=1)) if (use_wind and cfg.get("wind")) else {}
    stay_f, move_f = kernel_field(grid, sites, cfg, as_of, features, winds)
    kernel = stay_f + move_f
    learned = learned_habitat_weights(grid, sites) if habitat in ("learned", "blend") and len(sites) >= 10 else {}
    if habitat == "hand" or (habitat in ("learned", "blend") and not learned):
        weights = cfg["habitat"]
    elif habitat == "learned":
        weights = learned
    elif habitat == "blend":
        keys = set(cfg["habitat"]) | set(learned)
        weights = {k: 0.5 * cfg["habitat"].get(k, 0.0) + 0.5 * learned.get(k, 0.0) for k in keys}
    else:
        weights = {}
    onshore_k = cfg.get("wind", {}).get("onshore", 0.0) if winds.get("day") is not None else 0.0
    hab = (habitat_field(grid, weights, cfg.get("coast_pref", 0.0), cfg.get("coast_scale_km", 20.0),
                         onshore_k, winds.get("day")) if weights else np.ones_like(kernel))
    g = cfg.get("kernel_gamma", 1.0) if gamma is None else gamma
    if cfg.get("combine", "mult") == "additive":
        score = combine_additive(stay_f, move_f, hab, grid, g, cfg.get("c_move", 0.7))
    else:
        score = combine(kernel, hab, grid, g)
    if anchor:
        score = anchor_sites(score, grid, sites, as_of, cfg.get("anchor_km", 3.0))
    # Version 3: absolute chance of a report per cell tomorrow (the map and the ranking use this).
    n_new = staymod.new_site_rate(reports, cfg, as_of) if sites else 0.0
    lam, prob, p_sites, lam_new = probability_field(grid, sites, cfg, as_of, move_f, hab, stay_model, n_new, g)
    wind_info = {"used": bool(winds), "drift": cfg.get("wind", {}).get("drift", 0.0) if winds.get("night") is not None else 0.0,
                 "onshore": onshore_k, "night": winds.get("night_summary"), "day": winds.get("day_summary")}
    return {"as_of": as_of, "reports": sp, "sites": sites, "kernel": kernel, "stay": stay_f, "move": move_f,
            "habitat": hab, "habitat_weights": weights, "learned_weights": learned, "score_rank": score,
            "lambda": lam, "lam_new": lam_new, "prob": prob, "score": prob, "p_sites": p_sites, "n_new": n_new,
            "wind": wind_info}


# ----------------------------------------------------------------------------
# Rendering and outputs
# ----------------------------------------------------------------------------

# Autumn-leaf palette (RGBA) for the chance layer, moss greens for the new-site
# ranking: the map should read as nature, not as an instrument panel.
PALETTE = {1: (242, 217, 166, 105), 2: (227, 168, 95, 140), 3: (201, 112, 46, 170), 4: (138, 61, 18, 200)}
PALETTE_NEW = {1: (198, 224, 196, 95), 2: (140, 190, 150, 125), 3: (80, 150, 100, 150), 4: (30, 100, 60, 175)}


def _merc_y(lat_deg: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2))


def render_png(cls: np.ndarray, grid: dict, path: str, scale: int = 2, palette: dict | None = None) -> None:
    """Draw the class grid as a Web Mercator image so Leaflet's imageOverlay
    places every row at the right latitude."""
    from PIL import Image
    lat_top, lat_bot = float(grid["lat_edges"][0]), float(grid["lat_edges"][-1])
    lon_l, lon_r = float(grid["lon_edges"][0]), float(grid["lon_edges"][-1])
    width = grid["nx"] * scale
    y_top, y_bot = _merc_y(lat_top), _merc_y(lat_bot)
    px = math.radians(lon_r - lon_l) / width
    height = int(round((y_top - y_bot) / px))
    rows = np.arange(height)
    y = y_top - (rows + 0.5) * px
    lat = np.degrees(2 * np.arctan(np.exp(y)) - math.pi / 2)
    iy = np.clip(((lat_top - lat) / grid["dlat"]).astype(int), 0, grid["ny"] - 1)
    cols = np.arange(width)
    lon = lon_l + (cols + 0.5) * (lon_r - lon_l) / width
    ix = np.clip(((lon - lon_l) / grid["dlon"]).astype(int), 0, grid["nx"] - 1)
    sampled = cls[iy][:, ix]
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    for lvl, col in (palette or PALETTE).items():
        rgba[sampled == lvl] = col
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def grid_meta(grid: dict) -> dict:
    """What the page needs to map a click to a cell."""
    return {"lat0": float(grid["lat_edges"][0]), "lon0": float(grid["lon_edges"][0]),
            "dlat": grid["dlat"], "dlon": grid["dlon"], "ny": grid["ny"], "nx": grid["nx"]}


def sparse_cells(prob: np.ndarray, grid: dict, min_p: float = 0.01) -> list:
    """[[row, col, chance]] for every land cell at or above min_p."""
    iy, ix = np.nonzero((prob >= min_p) & grid["land"])
    return [[int(a), int(b), round(float(prob[a, b]), 3)] for a, b in zip(iy, ix)]


def _site_json(s: dict, cfg: dict, as_of: date, p_fit: float | None = None) -> dict:
    subs = sorted(s["subspecies"])
    w, p = site_weight(s, cfg, as_of)
    return {"lat": round(s["lat"], 5), "lon": round(s["lon"], 5), "n": s["n_reports"], "days": s["n_days"],
            "first": s["first_date"].isoformat(), "last": s["last_date"].isoformat(),
            "max_count": s["max_count"], "photos": s.get("n_photo", 0), "weight": round(w, 2),
            "p_stay": round(p_fit if p_fit is not None else p, 3), "loc": s["loc"], "sub": subs}


def read_backtest_summary(slug: str) -> dict | None:
    path = os.path.join(OUT_DIR, f"{slug}-backtest.csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None

    def avg(col, n_col):
        if col not in rows[0]:
            return None
        pairs = [(float(r[col]), float(r[n_col])) for r in rows if r.get(col) not in (None, "", "nan")]
        n = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / n, 3) if n else None

    out = {"days": len(rows), "first": rows[0]["as_of"], "last": rows[-1]["as_of"],
           "n_all_total": int(sum(float(r["n_all"]) for r in rows)),
           "n_new_total": int(sum(float(r["n_new"]) for r in rows)),
           "persist_share": round(sum(float(r["persist_share"]) for r in rows) / len(rows), 3)}
    for v in ("model", "new_rank", "nowind", "persist", "habitat_only", "generic"):
        for g in ("all", "new"):
            out[f"{v}_{g}"] = avg(f"{v}_{g}", f"n_{g}")
    return out


def write_outputs(fc: dict, grid: dict, cfg: dict, slug: str) -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    cls, counts = prob_classes(fc["prob"], grid["land"])
    render_png(cls, grid, os.path.join(OUT_DIR, f"{slug}.png"))
    files = {"image": f"/forecast/{slug}.png"}
    new_counts = {}
    new_path = os.path.join(OUT_DIR, f"{slug}-nieuw.png")
    if fc["lam_new"][grid["land"]].max() > 0:
        cls_new, new_counts = rank_classes(fc["lam_new"], grid["land"])
        render_png(cls_new, grid, new_path, palette=PALETTE_NEW)
        files["image_new"] = f"/forecast/{slug}-nieuw.png"
    elif os.path.exists(new_path):
        os.remove(new_path)
    recent = [s for s in fc["sites"] if (fc["as_of"] - s["last_date"]).days <= 2]
    order = sorted(range(len(fc["sites"])), key=lambda i: fc["sites"][i]["last_date"], reverse=True)
    meta = {
        "species": slug, "label": cfg["label"], "names": cfg["names"], "sci": cfg.get("sci", ""),
        "guild": cfg["guild"], "guild_label": cfg["guild_label"], "rarity_class": cfg.get("rarity_class", ""),
        "source": cfg.get("source", ""),
        "as_of": fc["as_of"].isoformat(), "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bounds": [[float(grid["lat_edges"][-1]), float(grid["lon_edges"][0])],
                   [float(grid["lat_edges"][0]), float(grid["lon_edges"][-1])]],
        "window_days": cfg["window_days"], "n_reports": len(fc["reports"]), "n_sites": len(fc["sites"]),
        "n_sites_recent": len(recent),
        "profile": cfg.get("profile", ""),
        "params": {k: cfg.get(k) for k in ("site_km", "decay_days", "p_stay", "sigma_stay_km", "scale_move_km",
                                           "kernel_gamma", "coast_pref", "combine", "anchor_km")},
        "wind": fc["wind"],
        "habitat_weights": {k: round(v, 3) for k, v in fc["habitat_weights"].items()},
        "learned_weights": {k: round(v, 2) for k, v in (fc.get("learned_weights") or {}).items()},
        "classes": counts, "new_classes": new_counts, "areas": area_chances(fc["lambda"], grid),
        "mode": "probability", "thresholds": PROB_THRESHOLDS, "grid": grid_meta(grid),
        "cells": sparse_cells(fc["prob"], grid),
        "max_p": round(float(fc["prob"][grid["land"]].max()), 3) if grid["land"].any() else 0.0,
        "expected_reports": round(float(fc["lambda"][grid["land"]].sum()), 2),
        "expected_new_sites": round(float(fc["n_new"]), 2),
        "sites": [_site_json(fc["sites"][i], cfg, fc["as_of"], fc["p_sites"][i]) for i in order],
        "backtest": read_backtest_summary(slug),
        "files": files,
    }
    with open(os.path.join(OUT_DIR, f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def write_empty(cfg: dict, slug: str, as_of: date) -> dict:
    """JSON for a species without reports in the window: listed, no map."""
    os.makedirs(OUT_DIR, exist_ok=True)
    for stale in (os.path.join(OUT_DIR, f"{slug}.png"), os.path.join(OUT_DIR, f"{slug}-nieuw.png")):
        if os.path.exists(stale):
            os.remove(stale)
    meta = {"species": slug, "label": cfg["label"], "names": cfg["names"], "sci": cfg.get("sci", ""),
            "guild": cfg["guild"], "guild_label": cfg["guild_label"], "rarity_class": cfg.get("rarity_class", ""),
            "source": cfg.get("source", ""), "as_of": as_of.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window_days": cfg["window_days"], "n_reports": 0, "n_sites": 0, "n_sites_recent": 0,
            "profile": cfg.get("profile", ""),
            "params": {k: cfg.get(k) for k in ("site_km", "decay_days", "p_stay", "sigma_stay_km", "scale_move_km",
                                               "kernel_gamma", "coast_pref")},
            "wind": {"used": False}, "habitat_weights": {}, "learned_weights": {}, "classes": {}, "sites": [],
            "mode": "probability", "thresholds": PROB_THRESHOLDS, "max_p": 0.0, "expected_reports": 0.0, "expected_new_sites": 0.0,
            "backtest": read_backtest_summary(slug), "files": {}}
    with open(os.path.join(OUT_DIR, f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def write_index(entries: list[dict]) -> None:
    """out/index.json: all species the page can switch between, plus the guilds."""
    guilds = {g: {"label": gp["label"], "method": gp["method"],
                  "params": {k: gp[k] for k in ("p_stay", "sigma_stay_km", "scale_move_km", "decay_days", "coast_pref")},
                  "wind": gp["wind"], "habitat": gp["habitat"]} for g, gp in GUILDS.items()}
    combined = [e for e in entries if e["species"] == "alle"]
    entries = combined + sorted([e for e in entries if e["species"] != "alle"], key=lambda e: (-e["n_reports"], e["label"]))
    calib = stay_model = None
    for name in ("calibration.json", "stay-model.json"):
        pth = os.path.join(OUT_DIR, name)
        if os.path.exists(pth):
            with open(pth, encoding="utf-8") as f:
                if name.startswith("calibration"):
                    calib = json.load(f)
                else:
                    stay_model = json.load(f)
    with open(os.path.join(OUT_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "species": entries, "guilds": guilds, "calibration": calib,
                   "stay_model": {k: v for k, v in (stay_model or {}).items() if k != "weights"} or None,
                   "thresholds": PROB_THRESHOLDS}, f, ensure_ascii=False, indent=1)


def index_entry(meta: dict) -> dict:
    b = meta.get("backtest") or {}
    return {"species": meta["species"], "label": meta["label"], "sci": meta.get("sci", ""), "guild": meta["guild"],
            "guild_label": meta["guild_label"], "rarity_class": meta.get("rarity_class", ""), "source": meta.get("source", ""),
            "as_of": meta["as_of"], "n_sites": meta["n_sites"], "n_reports": meta["n_reports"],
            "n_sites_recent": meta["n_sites_recent"], "profile": meta.get("profile", ""),
            "has_map": bool(meta.get("files", {}).get("image")), "wind_used": bool(meta["wind"].get("used")),
            "max_p": meta.get("max_p", 0.0), "expected_reports": meta.get("expected_reports", 0.0),
            "n_species": meta.get("n_species"),
            "backtest": {k: b.get(k) for k in ("days", "n_all_total", "n_new_total", "model_all", "model_new",
                                               "nowind_new", "persist_all", "persist_new", "habitat_only_new")} if b else None}


def new_accumulator(grid: dict) -> dict:
    return {"lam": np.zeros((grid["ny"], grid["nx"]), dtype=np.float64), "lam_new": np.zeros((grid["ny"], grid["nx"]), dtype=np.float64),
            "per_species": {}, "labels": {}, "expected": {}, "sites": []}


def write_combined(acc: dict, grid: dict, as_of: date, top_n: int = 3) -> dict:
    """All species in one map: the chance of any rare-species report per cell,
    with the species that contribute most to each cell."""
    os.makedirs(OUT_DIR, exist_ok=True)
    land = grid["land"]
    prob = 1 - np.exp(-acc["lam"])
    prob[~land] = 0.0
    cls, counts = prob_classes(prob, land)
    render_png(cls, grid, os.path.join(OUT_DIR, "alle.png"))
    files = {"image": "/forecast/alle.png"}
    new_counts = {}
    if acc["lam_new"][land].max() > 0:
        cls_new, new_counts = rank_classes(acc["lam_new"], land)
        render_png(cls_new, grid, os.path.join(OUT_DIR, "alle-nieuw.png"), palette=PALETTE_NEW)
        files["image_new"] = "/forecast/alle-nieuw.png"
    slugs = list(acc["per_species"])
    stack = np.stack([acc["per_species"][k] for k in slugs]) if slugs else np.zeros((0, grid["ny"], grid["nx"]), dtype=np.float32)
    cells = []
    iy, ix = np.nonzero((prob >= 0.01) & land)
    for a, b in zip(iy, ix):
        contrib = stack[:, a, b] if len(slugs) else np.array([])
        order = np.argsort(-contrib)[:top_n]
        top = [[slugs[i], acc["labels"][slugs[i]], round(float(1 - math.exp(-contrib[i])), 3)] for i in order if contrib[i] > 0.002]
        cells.append([int(a), int(b), round(float(prob[a, b]), 3), top])
    # per municipality: total chance and the species that contribute most
    n_gem = len(grid["gem_names"])
    tot = np.bincount(grid["gem"].ravel(), weights=acc["lam"].ravel(), minlength=n_gem + 1)
    per = np.stack([np.bincount(grid["gem"].ravel(), weights=acc["per_species"][k].ravel().astype(np.float64), minlength=n_gem + 1)
                    for k in slugs]) if slugs else np.zeros((0, n_gem + 1))
    areas = []
    for i, name in enumerate(grid["gem_names"], start=1):
        p = 1 - math.exp(-float(tot[i]))
        if p < 0.005:
            continue
        order = np.argsort(-per[:, i])[:5] if len(slugs) else []
        top = [[slugs[j], acc["labels"][slugs[j]], round(float(1 - math.exp(-per[j, i])), 3)] for j in order if per[j, i] > 0.002]
        areas.append({"name": name, "p": round(p, 3), "expected": round(float(tot[i]), 2), "top": top})
    areas.sort(key=lambda a: -a["p"])
    exp_sorted = sorted(acc["expected"].items(), key=lambda kv: -kv[1][0])
    meta = {
        "species": "alle", "label": "Alle zeldzame soorten", "guild": "alle", "guild_label": "alle methodegroepen samen",
        "rarity_class": "", "source": "combined", "as_of": as_of.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bounds": [[float(grid["lat_edges"][-1]), float(grid["lon_edges"][0])],
                   [float(grid["lat_edges"][0]), float(grid["lon_edges"][-1])]],
        "window_days": 7, "n_species": len(slugs), "n_reports": sum(1 for _ in acc["sites"]),
        "n_sites": len(acc["sites"]), "n_sites_recent": len(acc["sites"]),
        "profile": "Alle zeldzame en zeer zeldzame soorten opgeteld: per hok de kans dat er morgen minstens één melding komt, "
                   "en welke soorten daar het meest aan bijdragen. Tik op een hok.",
        "params": {}, "wind": {"used": False}, "habitat_weights": {}, "learned_weights": {},
        "mode": "probability", "thresholds": PROB_THRESHOLDS, "grid": grid_meta(grid), "cells": cells,
        "max_p": round(float(prob[land].max()), 3) if land.any() else 0.0,
        "expected_reports": round(float(acc["lam"][land].sum()), 2),
        "expected_new_sites": round(sum(v[1] for v in acc["expected"].values()), 2),
        "top_species": [{"species": k, "label": acc["labels"][k], "expected_reports": v[0], "expected_new_sites": v[1]}
                        for k, v in exp_sorted[:12]],
        "classes": counts, "new_classes": new_counts, "areas": areas,
        "sites": sorted(acc["sites"], key=lambda x: -x.get("p_stay", 0)),
        "backtest": None, "files": files,
    }
    with open(os.path.join(OUT_DIR, "alle.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def fit_stay_model(reports: list[dict], registry: dict) -> dict:
    """Fit the stay model on the archive and store it next to the layers."""
    os.makedirs(OUT_DIR, exist_ok=True)
    m = staymod.fit(reports, registry)
    with open(os.path.join(OUT_DIR, "stay-model.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    return m


def run_species(reports: list[dict], grid: dict, registry: dict, slugs: list[str], latest: date, wdata: dict | None,
                habitat: str = "blend", as_of_fixed: date | None = None, verbose: bool = False,
                stay_model: dict | None = None, acc: dict | None = None) -> list[dict]:
    """Build (or empty) every species in slugs; returns the index entries. With acc,
    the expected-report fields and recent sites are collected for the combined map."""
    entries = []
    for slug in slugs:
        cfg = registry[slug]
        sp_dates = [parse_date(r["date"]) for r in reports if r["key"] in cfg["name_keys"]]
        as_of = as_of_fixed or latest          # "tomorrow" is the same day for every species
        if not sp_dates or (as_of - max(sp_dates)).days >= cfg["window_days"]:
            meta = write_empty(cfg, slug, as_of)
        else:
            fc = build_forecast(reports, grid, cfg, as_of, habitat=habitat, wdata=wdata, stay_model=stay_model)
            meta = write_outputs(fc, grid, cfg, slug)
            if acc is not None:
                acc["lam"] += fc["lambda"]
                acc["lam_new"] += fc["lam_new"]
                acc["per_species"][slug] = fc["lambda"].astype(np.float32)
                acc["labels"][slug] = cfg["label"]
                acc["expected"][slug] = (meta["expected_reports"], meta["expected_new_sites"])
                for sj in meta["sites"]:
                    if (as_of - date.fromisoformat(sj["last"])).days <= 2:
                        acc["sites"].append({**sj, "species": slug, "label": cfg["label"]})
            if verbose:
                print(f"{cfg['label']:28} [{cfg['guild']}] as of {as_of}: {meta['n_reports']:4d} reports, {meta['n_sites']:3d} sites, "
                      f"max chance {100 * meta['max_p']:.0f}%, expected reports {meta['expected_reports']}, new sites {meta['expected_new_sites']}")
        entries.append(index_entry(meta))
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--species", default="all", help="slug or 'all' (every rare or very rare species in the data)")
    ap.add_argument("--as-of", default=None, help="ISO date; default: latest report date of the species")
    ap.add_argument("--habitat", default="blend", choices=["blend", "hand", "learned", "none"])
    ap.add_argument("--no-wind", action="store_true")
    args = ap.parse_args()
    reports = load_sightings()
    grid = load_grid()
    registry = species_registry(reports)
    slugs = sorted(registry) if args.species == "all" else [args.species]
    latest = max(parse_date(r["date"]) for r in reports)
    wdata = None if args.no_wind else windmod.load(need_until=latest + timedelta(days=1))
    stay_model = fit_stay_model(reports, registry)
    print(f"stay model: {stay_model.get('n')} site-days, base rate {stay_model.get('base_rate')}, Brier {stay_model.get('brier')} vs {stay_model.get('brier_base')}")
    acc = new_accumulator(grid) if args.species == "all" else None
    entries = run_species(reports, grid, registry, slugs, latest, wdata, args.habitat,
                          date.fromisoformat(args.as_of) if args.as_of else None, verbose=True, stay_model=stay_model, acc=acc)
    if acc is not None:
        cm = write_combined(acc, grid, date.fromisoformat(args.as_of) if args.as_of else latest)
        entries.insert(0, index_entry(cm))
        print(f"combined map: {cm['n_species']} species, expected {cm['expected_reports']} reports, max chance {100 * cm['max_p']:.0f}%")
    if args.species == "all":
        write_index(entries)
    else:
        # keep the other species in the index
        path = os.path.join(OUT_DIR, "index.json")
        old = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = [e for e in json.load(f).get("species", []) if e["species"] not in slugs]
        write_index(old + entries)
    print("written to", OUT_DIR)


if __name__ == "__main__":
    main()
