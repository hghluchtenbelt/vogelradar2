"""Wind for the activity forecast: Open-Meteo 10 m wind on a coarse grid over
the Netherlands, cached per fetch day, interpolated onto the model grid.

Open-Meteo free tier (non-commercial, CC BY 4.0): 16 calls per refresh.
Night wind (18:00 to 06:00 UTC) drives the downwind shift of migrants,
day wind (06:00 to 18:00 UTC) the onshore boost for seabirds.
"""
from __future__ import annotations

import glob
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "wind")
LATS = [51.0, 51.8, 52.6, 53.4]
LONS = [3.6, 4.7, 5.8, 6.9]
API = "https://api.open-meteo.com/v1/forecast"


def fetch(past_days: int = 10, forecast_days: int = 3) -> dict:
    """Download hourly wind for the 4 x 4 grid and cache it as data/wind/<today>.json."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    points = []
    for lat in LATS:
        for lon in LONS:
            q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "hourly": "wind_speed_10m,wind_direction_10m",
                                        "wind_speed_unit": "ms", "timezone": "UTC", "past_days": past_days,
                                        "forecast_days": forecast_days})
            req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "vogelradar-forecast/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            h = d["hourly"]
            points.append({"lat": lat, "lon": lon, "time": h["time"], "speed": h["wind_speed_10m"], "dir": h["wind_direction_10m"]})
    data = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "points": points}
    with open(os.path.join(CACHE_DIR, f"{date.today().isoformat()}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def load(need_until: date | None = None, allow_fetch: bool = True) -> dict | None:
    """Latest cache; refetch when it does not cover need_until (or is absent)."""
    files = sorted(glob.glob(os.path.join(CACHE_DIR, "*.json")))
    data = None
    if files:
        with open(files[-1], encoding="utf-8") as f:
            data = json.load(f)
    if data and need_until:
        last = data["points"][0]["time"][-1][:10]
        if last < need_until.isoformat():
            data = None
    if data is None and allow_fetch:
        try:
            data = fetch()
        except Exception as e:  # offline: wind simply stays off
            print("wind: fetch failed,", e)
            data = None
    return data


def _uv(speed: float, direction: float) -> tuple[float, float]:
    """Meteorological 'from' direction to (east, north) components of the wind vector."""
    rad = math.radians(direction)
    return -speed * math.sin(rad), -speed * math.cos(rad)


def mean_uv(data: dict, start: datetime, end: datetime) -> tuple[np.ndarray, np.ndarray] | None:
    """Mean (u, v) per grid point over [start, end) UTC, shape (len(LATS), len(LONS))."""
    U = np.zeros((len(LATS), len(LONS))); V = np.zeros_like(U)
    for p in data["points"]:
        us, vs = [], []
        for t, s, d in zip(p["time"], p["speed"], p["dir"]):
            ts = datetime.fromisoformat(t)
            if start <= ts < end and s is not None and d is not None:
                u, v = _uv(s, d); us.append(u); vs.append(v)
        if not us:
            return None
        i, j = LATS.index(p["lat"]), LONS.index(p["lon"])
        U[i, j] = np.mean(us); V[i, j] = np.mean(vs)
    return U, V


def night_wind(data: dict, target_day: date):
    """Wind of the night before target_day (18:00 the day before to 06:00)."""
    d0 = datetime.combine(target_day - timedelta(days=1), datetime.min.time()) + timedelta(hours=18)
    return mean_uv(data, d0, d0 + timedelta(hours=12))


def day_wind(data: dict, target_day: date):
    d0 = datetime.combine(target_day, datetime.min.time()) + timedelta(hours=6)
    return mean_uv(data, d0, d0 + timedelta(hours=12))


def _bilinear(F: np.ndarray, lat_c: np.ndarray, lon_c: np.ndarray) -> np.ndarray:
    """Bilinear interpolation of a len(LATS) x len(LONS) field onto the grid,
    linearly extrapolated outside the 4 x 4 box (numpy only)."""
    la, lo = np.array(LATS), np.array(LONS)
    i = np.clip(np.searchsorted(la, lat_c) - 1, 0, len(la) - 2)
    j = np.clip(np.searchsorted(lo, lon_c) - 1, 0, len(lo) - 2)
    ty = (lat_c - la[i]) / (la[i + 1] - la[i])
    tx = (lon_c - lo[j]) / (lo[j + 1] - lo[j])
    TY, TX = np.meshgrid(ty, tx, indexing="ij")
    I, J = np.meshgrid(i, j, indexing="ij")
    return ((1 - TY) * (1 - TX) * F[I, J] + (1 - TY) * TX * F[I, J + 1]
            + TY * (1 - TX) * F[I + 1, J] + TY * TX * F[I + 1, J + 1])


def on_grid(grid: dict, U: np.ndarray, V: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear interpolation of the 4 x 4 fields onto the model grid."""
    return _bilinear(U, grid["lat_c"], grid["lon_c"]), _bilinear(V, grid["lat_c"], grid["lon_c"])


def summary(U: np.ndarray, V: np.ndarray) -> dict:
    u, v = float(U.mean()), float(V.mean())
    speed = math.hypot(u, v)
    frm = (math.degrees(math.atan2(-u, -v)) + 360) % 360
    return {"speed_ms": round(speed, 1), "from_deg": round(frm), "from": _compass(frm)}


def _compass(deg: float) -> str:
    names = ["N", "NO", "O", "ZO", "Z", "ZW", "W", "NW"]
    return names[int((deg + 22.5) // 45) % 8]
