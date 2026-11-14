"""Map a (lat, lng) to a Dutch municipality via local point-in-polygon
on official gemeente boundaries (gemeentes.geojson). No external lookups.
"""
from __future__ import annotations

import json
from pathlib import Path

_GEOJSON = Path(__file__).parent / "gemeentes.geojson"

# Each entry: (name, (min_lng, min_lat, max_lng, max_lat), [polygon, ...])
# where polygon = [outer_ring, hole_ring, ...] and ring = [[lng, lat], ...]
_areas: list[tuple] = []


def _load() -> None:
    data = json.loads(_GEOJSON.read_text(encoding="utf-8"))
    for f in data["features"]:
        name = f["properties"].get("statnaam")
        geom = f["geometry"]
        if geom["type"] == "Polygon":
            polys = [geom["coordinates"]]
        elif geom["type"] == "MultiPolygon":
            polys = geom["coordinates"]
        else:
            continue
        min_lng = min_lat = 1e9
        max_lng = max_lat = -1e9
        for poly in polys:
            for ring in poly:
                for lng, lat in ring:
                    min_lng = min(min_lng, lng); max_lng = max(max_lng, lng)
                    min_lat = min(min_lat, lat); max_lat = max(max_lat, lat)
        _areas.append((name, (min_lng, min_lat, max_lng, max_lat), polys))


def _in_ring(lng: float, lat: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
                (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _in_poly(lng: float, lat: float, poly: list) -> bool:
    if not poly or not _in_ring(lng, lat, poly[0]):
        return False
    for hole in poly[1:]:           # inside a hole → not in polygon
        if _in_ring(lng, lat, hole):
            return False
    return True


def _ring_centroid(ring: list) -> tuple[float, float, float]:
    """(lng, lat, area) of a ring, area-weighted (planar; fine at this scale)."""
    area = cx = cy = 0.0
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        cross = xj * yi - xi * yj
        area += cross
        cx += (xj + xi) * cross
        cy += (yj + yi) * cross
        j = i
    if not area:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys), 0.0
    return cx / (3 * area), cy / (3 * area), abs(area) / 2


# Everyday names for municipalities whose official name is rarely typed.
_ALIASES = {"'s-Gravenhage": "Den Haag", "'s-Hertogenbosch": "Den Bosch"}


def places() -> list[tuple[str, float, float]]:
    """(name, lat, lng) of every municipality: the centre of its largest
    part. Used by the website to let a visitor pick a location by name,
    without any external geocoder."""
    if not _areas:
        _load()
    out: list[tuple[str, float, float]] = []
    for name, _bbox, polys in _areas:
        lng, lat, _area = max(
            (_ring_centroid(poly[0]) for poly in polys if poly),
            key=lambda c: c[2],
        )
        out.append((name, round(lat, 4), round(lng, 4)))
        if name in _ALIASES:
            out.append((_ALIASES[name], round(lat, 4), round(lng, 4)))
    out.sort(key=lambda p: p[0].lstrip("'").casefold())
    return out


def gemeente_for(lat: float, lng: float) -> str | None:
    if not _areas:
        _load()
    for name, (mn_lng, mn_lat, mx_lng, mx_lat), polys in _areas:
        if lng < mn_lng or lng > mx_lng or lat < mn_lat or lat > mx_lat:
            continue
        for poly in polys:
            if _in_poly(lng, lat, poly):
                return name
    return None
