"""Empirical stay model: how likely is a reported site to be reported again
tomorrow? Fitted on the archived reports (site-days of every rare species).

Dataset: for every species, day D and site known on D (reports within 2 km
in the 7 days up to D): outcome = a report of the species within 3 km on
D+1; features = method group, days present, age of the last report, photo,
number of reports, number of birds. Model: logistic regression (numpy,
L2-regularised) on a handful of features; refit by the pipeline as the
archive grows. Also estimates the spread of re-reports around a site and
the new-site rate per species.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import timedelta

import numpy as np

from guilds import GUILDS

GUILD_LIST = list(GUILDS)
FEATURES = ["bias", "log_days", "age1", "age2plus", "photo", "log_reports", "flock"] + [f"g_{g}" for g in GUILD_LIST]


def site_features(site: dict, cfg: dict, as_of) -> np.ndarray:
    age = (as_of - site["last_date"]).days
    x = [1.0, math.log(site["n_days"]), 1.0 if age == 1 else 0.0, 1.0 if age >= 2 else 0.0,
         1.0 if site.get("n_photo", 0) > 0 else 0.0, math.log(site["n_reports"]),
         1.0 if site.get("max_count", 1) > 1 else 0.0]
    x += [1.0 if cfg["guild"] == g else 0.0 for g in GUILD_LIST]
    return np.array(x)


def build_dataset(reports: list[dict], registry: dict, min_rarity: int = 3):
    """Site-days with outcome, across all species with reports."""
    from model import cluster_sites, haversine_km, parse_date, species_reports
    X, y, meta, dists = [], [], [], []
    by_species = defaultdict(list)
    for r in reports:
        if r["rarity"] >= min_rarity:
            by_species[r["key"]].append(r)
    for slug, cfg in registry.items():
        sp = [r for k in cfg["name_keys"] for r in by_species.get(k, [])]
        if not sp:
            continue
        dates = sorted({parse_date(r["date"]) for r in sp})
        last = dates[-1]
        for as_of in dates:
            nxt = as_of + timedelta(days=1)
            if nxt > last:          # the day after the last archived day is unknown
                continue
            prior = species_reports(reports, cfg, as_of)
            if not prior:
                continue
            targets = [r for r in sp if parse_date(r["date"]) == nxt]
            tl = np.array([t["lat"] for t in targets]); tn = np.array([t["lon"] for t in targets])
            for s in cluster_sites(prior, cfg["site_km"]):
                if (as_of - s["last_date"]).days > 3:
                    continue
                d = haversine_km(s["lat"], s["lon"], tl, tn) if len(targets) else np.array([])
                again = bool(len(d) and d.min() <= 3.0)
                X.append(site_features(s, cfg, as_of)); y.append(1.0 if again else 0.0)
                meta.append({"species": slug, "guild": cfg["guild"], "as_of": as_of.isoformat(), "days": s["n_days"],
                             "age": (as_of - s["last_date"]).days, "photo": s.get("n_photo", 0) > 0})
                if again:
                    dists.extend(float(v) for v in d[d <= 3.0])
    return np.array(X), np.array(y), meta, dists


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 200) -> np.ndarray:
    """Newton iterations with ridge penalty (bias unpenalised)."""
    n, k = X.shape
    w = np.zeros(k)
    pen = np.full(k, l2); pen[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + pen * w
        W = p * (1 - p)
        H = (X * W[:, None]).T @ X + np.diag(pen)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def fit(reports: list[dict], registry: dict) -> dict:
    X, y, meta, dists = build_dataset(reports, registry)
    if len(y) < 30:
        return {"ok": False, "n": int(len(y))}
    w = fit_logistic(X, y)
    p = 1 / (1 + np.exp(-X @ w))
    # stratified rates for the explanation page
    def rate(mask):
        m = np.array(mask, dtype=bool)
        return {"n": int(m.sum()), "rate": round(float(y[m].mean()), 3) if m.any() else None}
    days = np.array([m["days"] for m in meta]); age = np.array([m["age"] for m in meta]); photo = np.array([m["photo"] for m in meta])
    guild = np.array([m["guild"] for m in meta])
    strata = {
        "by_days": {"1": rate(days == 1), "2": rate(days == 2), "3-4": rate((days >= 3) & (days <= 4)), "5+": rate(days >= 5)},
        "by_age": {"0": rate(age == 0), "1": rate(age == 1), "2-3": rate(age >= 2)},
        "by_photo": {"met": rate(photo), "zonder": rate(~photo)},
        "by_guild": {g: rate(guild == g) for g in GUILD_LIST if (guild == g).any()},
        "by_days_age0": {"1": rate((days == 1) & (age == 0)), "2": rate((days == 2) & (age == 0)),
                         "3-4": rate((days >= 3) & (days <= 4) & (age == 0)), "5+": rate((days >= 5) & (age == 0))},
    }
    spread_km = float(np.median(dists)) if dists else 0.5
    return {"ok": True, "n": int(len(y)), "base_rate": round(float(y.mean()), 3), "weights": dict(zip(FEATURES, [round(float(v), 4) for v in w])),
            "brier": round(float(np.mean((p - y) ** 2)), 4), "brier_base": round(float(np.mean((y.mean() - y) ** 2)), 4),
            "spread_median_km": round(spread_km, 2), "spread_p75_km": round(float(np.percentile(dists, 75)), 2) if dists else None,
            "strata": strata}


def predict(model: dict, site: dict, cfg: dict, as_of) -> float:
    if not model or not model.get("ok"):
        return cfg["p_stay"] * 0.5   # no fitted model yet: half the assumed value
    w = np.array([model["weights"][f] for f in FEATURES])
    return float(1 / (1 + np.exp(-(site_features(site, cfg, as_of) @ w))))


def new_site_rate(reports: list[dict], cfg: dict, as_of, registry_rates: dict | None = None) -> float:
    """Expected number of new sites tomorrow: new sites per day over the last 3
    days, shrunk towards the species' 7-day mean."""
    from model import cluster_sites, haversine_km, parse_date, species_reports
    sp = species_reports(reports, cfg, as_of)
    if not sp:
        return 0.0
    dates = sorted({parse_date(r["date"]) for r in sp})
    counts = []
    for d in dates:
        before = [r for r in sp if parse_date(r["date"]) < d]
        today = [r for r in sp if parse_date(r["date"]) == d]
        new = 0
        if before:
            bl = np.array([r["lat"] for r in before]); bn = np.array([r["lon"] for r in before])
            seen = []
            for r in today:
                if haversine_km(r["lat"], r["lon"], bl, bn).min() > cfg["site_km"] and all(
                        haversine_km(r["lat"], r["lon"], s[0], s[1]) > cfg["site_km"] for s in seen):
                    seen.append((r["lat"], r["lon"])); new += 1
        else:
            new = len(cluster_sites(today, cfg["site_km"]))
        counts.append((d, new))
    mean7 = sum(n for _, n in counts) / cfg["window_days"]
    recent = [n for d, n in counts if (as_of - d).days <= 2]
    last3 = sum(recent) / 3.0
    return 0.6 * last3 + 0.4 * mean7


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from model import load_sightings, species_registry
    reports = load_sightings()
    reg = species_registry(reports)
    m = fit(reports, reg)
    print(json.dumps({k: v for k, v in m.items() if k != "weights"}, indent=1, ensure_ascii=False))
    print("weights:", m.get("weights"))
