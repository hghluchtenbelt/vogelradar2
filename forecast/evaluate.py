"""Rolling-origin backtest of the "activity now" forecast, per species.

For every as-of day D with reports on D+1: build the forecast from the data
as known up to D (reports dated <= D, 7-day window) and score it against the
reports of D+1. Targets: all reports; new (farther than 2 km from any
same-species report in the 7 days up to D); far (farther than 10 km).

Metric: top-5 percent hit rate, the share of target reports whose 1 km cell
is in the top 5 percent of Dutch land cells. Variants: model (group
parameters, blended habitat, wind where the group uses it), nowind (same
without wind), persist (cells within 3 km of a report in the last 3 days),
habitat_only (static habitat map); with --full also basic (no day/photo/
count weights), hand_only, learned_only, kernel_only and generic (the
Nutcracker settings applied to this species).

Run:  .venv/bin/python forecast/evaluate.py --species all --quiet
Writes out/<slug>-backtest.csv per species and out/backtest-summary.csv.
Caveats: the newest snapshot day is partial (hourly feed); past wind comes
from Open-Meteo's archived analysis, not from the forecast issued on day D.
"""
from __future__ import annotations

import argparse
import csv
import os
from datetime import timedelta

import numpy as np

import wind as windmod
from guilds import GUILDS
import json
from model import (OUT_DIR, build_forecast, cell_index, cluster_sites, discover_species,
                   haversine_km, load_grid, load_sightings, parse_date, species_config, species_reports,
                   top_fraction_mask)
CLASS_LEVELS = {"top1": 4, "top5": 3, "top10": 2, "top25": 1}

TOP_Q = 0.05
QUICK = ["model", "new_rank", "rank_v2", "nowind", "persist", "habitat_only"]
PROB_BINS = [("p30", 0.30, 1.01), ("p10", 0.10, 0.30), ("p3", 0.03, 0.10), ("p1", 0.01, 0.03), ("p0", 0.0, 0.01)]
FULL = QUICK + ["basic", "hand_only", "learned_only", "kernel_only", "generic"]
GROUPS = ("all", "new", "far")


def _cells(grid, reports):
    out = []
    for r in reports:
        c = cell_index(grid, r["lat"], r["lon"])
        if c is not None and grid["land"][c]:
            out.append(c)
    return out


def hit_rate(mask: np.ndarray, cells: list) -> float:
    if not cells:
        return float("nan")
    return sum(1 for c in cells if mask[c]) / len(cells)


def persistence_mask(grid, recent_reports, radius_km=3.0) -> np.ndarray:
    mask = np.zeros((grid["ny"], grid["nx"]), dtype=bool)
    if not recent_reports:
        return mask
    lat2, lon2 = np.meshgrid(grid["lat_c"], grid["lon_c"], indexing="ij")
    for r in recent_reports:
        mask |= haversine_km(r["lat"], r["lon"], lat2, lon2) <= radius_km
    return mask & grid["land"]


def min_dist_km(r, others) -> float:
    if not others:
        return float("inf")
    lat = np.array([o["lat"] for o in others]); lon = np.array([o["lon"] for o in others])
    return float(haversine_km(r["lat"], r["lon"], lat, lon).min())


def backtest(slug: str, cfg: dict, reports: list, grid: dict, wdata, variants: list[str], verbose: bool,
             stay_models: dict | None = None, combined: dict | None = None) -> list[dict]:
    generic = dict(species_config("Notenkraker", cfg["names"]), label=cfg["label"], name_keys=cfg["name_keys"])
    sp_all = [r for r in reports if r["key"] in cfg["name_keys"]]
    dates = sorted({parse_date(r["date"]) for r in sp_all})
    rows = []
    for as_of in dates[1:]:
        nxt = as_of + timedelta(days=1)
        if nxt not in dates:
            continue
        targets = [r for r in sp_all if parse_date(r["date"]) == nxt]
        prior = species_reports(reports, cfg, as_of)
        if not prior or not targets:
            continue
        recent = [r for r in prior if (as_of - parse_date(r["date"])).days <= 2]
        sm = (stay_models or {}).get(as_of.isoformat())
        fc = build_forecast(reports, grid, cfg, as_of, wdata=wdata, stay_model=sm)
        masks = {"model": top_fraction_mask(fc["prob"], grid["land"], TOP_Q),
                 "new_rank": top_fraction_mask(fc["lam_new"], grid["land"], TOP_Q) if fc["lam_new"][grid["land"]].max() > 0 else np.zeros(fc["prob"].shape, dtype=bool),
                 "rank_v2": top_fraction_mask(fc["score_rank"], grid["land"], TOP_Q),
                 "habitat_only": top_fraction_mask(fc["habitat"], grid["land"], TOP_Q),
                 "persist": persistence_mask(grid, recent)}
        if "nowind" in variants:
            masks["nowind"] = top_fraction_mask(build_forecast(reports, grid, cfg, as_of, wdata=None, stay_model=sm)["prob"], grid["land"], TOP_Q)
        if "basic" in variants:
            masks["basic"] = top_fraction_mask(build_forecast(reports, grid, cfg, as_of, features=frozenset(), wdata=wdata)["score"], grid["land"], TOP_Q)
        if "hand_only" in variants:
            masks["hand_only"] = top_fraction_mask(build_forecast(reports, grid, cfg, as_of, habitat="hand", wdata=wdata)["score"], grid["land"], TOP_Q)
        if "learned_only" in variants:
            masks["learned_only"] = top_fraction_mask(build_forecast(reports, grid, cfg, as_of, habitat="learned", wdata=wdata)["score"], grid["land"], TOP_Q)
        if "kernel_only" in variants:
            masks["kernel_only"] = top_fraction_mask(build_forecast(reports, grid, cfg, as_of, habitat="none", wdata=wdata)["score"], grid["land"], TOP_Q)
        if "generic" in variants:
            masks["generic"] = top_fraction_mask(build_forecast(reports, grid, generic, as_of, habitat="hand", wdata=None)["score"], grid["land"], TOP_Q)
        groups = {"all": targets,
                  "new": [r for r in targets if min_dist_km(r, prior) > cfg["site_km"]],
                  "far": [r for r in targets if min_dist_km(r, prior) > 10.0]}
        row = {"species": slug, "guild": cfg["guild"], "as_of": as_of.isoformat(), "target_day": nxt.isoformat(),
               "n_prior_sites": len(cluster_sites(prior, cfg["site_km"])),
               "n_all": len(groups["all"]), "n_new": len(groups["new"]), "n_far": len(groups["far"]),
               "persist_share": round(float(masks["persist"][grid["land"]].mean()), 4),
               "wind_used": int(bool(fc["wind"].get("used")))}
        for v in variants:
            for g in GROUPS:
                row[f"{v}_{g}"] = round(hit_rate(masks[v], _cells(grid, groups[g])), 3)
        # Reliability per probability class: predicted chance vs observed share of cells with a report.
        cells_all = _cells(grid, groups["all"]); cells_new = _cells(grid, groups["new"])
        hit = np.zeros(fc["prob"].shape, dtype=bool)
        for c in cells_all:
            hit[c] = True
        land = grid["land"]
        if combined is not None:
            day = combined.setdefault(nxt.isoformat(), {"lam": np.zeros(fc["prob"].shape), "hit": np.zeros(fc["prob"].shape, dtype=bool),
                                                        "lam_new": np.zeros(fc["prob"].shape), "new_cells": []})
            day["lam"] += fc["lambda"]; day["hit"] |= hit
            day["lam_new"] += fc["lam_new"]; day["new_cells"] += cells_new
        row["brier"] = round(float(np.mean((fc["prob"][land] - hit[land]) ** 2)), 6)
        row["brier_zero"] = round(float(np.mean(hit[land] ** 2)), 6)
        row["stay_model_rows"] = int((sm or {}).get("n", 0))
        for name, lo, hi in PROB_BINS:
            in_bin = land & (fc["prob"] >= lo) & (fc["prob"] < hi)
            row[f"{name}_cells"] = int(in_bin.sum())
            row[f"{name}_hitcells"] = int((in_bin & hit).sum())
            row[f"{name}_sump"] = round(float(fc["prob"][in_bin].sum()), 3)
            row[f"{name}_recall_all"] = round(hit_rate(in_bin, cells_all), 3)
            row[f"{name}_recall_new"] = round(hit_rate(in_bin, cells_new), 3)
        # Apparent stay: sites reported on D that are reported again within 3 km on D+1.
        today_sites = cluster_sites([r for r in prior if parse_date(r["date"]) == as_of], cfg["site_km"])
        stayed = sum(1 for s_ in today_sites if min_dist_km(s_, targets) <= 3.0)
        row["stay_sites"] = len(today_sites); row["stay_again"] = stayed
        rows.append(row)
        if verbose:
            print(f"  {as_of} -> {nxt}: sites {row['n_prior_sites']:3d} | all {row['n_all']:3d} new {row['n_new']:3d} far {row['n_far']:3d} | "
                  + "  ".join(f"{v} {row[f'{v}_all']:.2f}/{row[f'{v}_new']:.2f}" for v in variants))
    return rows


def weighted(rows: list[dict], v: str, g: str):
    n = sum(r[f"n_{g}"] for r in rows)
    if not n:
        return None
    return sum(r[f"{v}_{g}"] * r[f"n_{g}"] for r in rows if r.get(f"{v}_{g}") == r.get(f"{v}_{g}")) / n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--species", default="all")
    ap.add_argument("--full", action="store_true", help="all variants (slower)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-wind", action="store_true")
    args = ap.parse_args()
    variants = FULL if args.full else QUICK
    reports = load_sightings()
    grid = load_grid()
    registry = discover_species(reports)
    slugs = sorted(registry) if args.species == "all" else [args.species]
    latest = max(parse_date(r["date"]) for r in reports)
    wdata = None if args.no_wind else windmod.load(need_until=latest)
    os.makedirs(OUT_DIR, exist_ok=True)
    # Stay models per as-of day on an expanding window (only site-days before that day);
    # with fewer than 150 rows the full in-sample fit is used and flagged in the CSV.
    import stay as staymod
    full_reg = discover_species(reports)
    X, y, meta_rows, _ = staymod.build_dataset(reports, full_reg)
    full = staymod.fit(reports, full_reg)
    stay_models = {}
    for d in sorted({m["as_of"] for m in meta_rows}):
        mask = np.array([m["as_of"] < d for m in meta_rows])
        if mask.sum() >= 150:
            w = staymod.fit_logistic(X[mask], y[mask])
            stay_models[d] = {"ok": True, "n": int(mask.sum()), "weights": dict(zip(staymod.FEATURES, [float(v) for v in w]))}
        else:
            stay_models[d] = dict(full, in_sample=True)
    print("stay models per day:", {d: m["n"] for d, m in stay_models.items()})
    summary = []
    combined_days: dict = {}
    for slug in slugs:
        cfg = registry[slug]
        rows = backtest(slug, cfg, reports, grid, wdata, variants, verbose=not args.quiet, stay_models=stay_models,
                        combined=combined_days)
        if not rows:
            continue
        with open(os.path.join(OUT_DIR, f"{slug}-backtest.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        s = {"species": slug, "label": cfg["label"], "guild": cfg["guild"], "days": len(rows),
             "n_all": sum(r["n_all"] for r in rows), "n_new": sum(r["n_new"] for r in rows), "n_far": sum(r["n_far"] for r in rows),
             "persist_share": round(sum(r["persist_share"] for r in rows) / len(rows), 4)}
        for v in variants:
            for g in GROUPS:
                h = weighted(rows, v, g)
                s[f"{v}_{g}"] = None if h is None else round(h, 3)
        summary.append(s)
        print(f"{cfg['label']:24} [{cfg['guild']:24}] days {len(rows)} all {s['n_all']:4d} new {s['n_new']:3d} | "
              + "  ".join(f"{v} {100 * (s[f'{v}_all'] or 0):3.0f}%/{100 * (s[f'{v}_new'] or 0):3.0f}%" for v in variants))
    if not summary:
        return
    path = os.path.join(OUT_DIR, "backtest-summary.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys())); w.writeheader(); w.writerows(summary)
    f_ = lambda x: "  - " if x is None else f"{100 * x:3.0f}%"

    def pooled(rows, v, g):
        n = sum(r[f"n_{g}"] for r in rows)
        return None if not n else sum((r[f"{v}_{g}"] or 0) * r[f"n_{g}"] for r in rows) / n

    # calibration.json: per guild and overall, per legend class
    all_rows = []
    for slug in slugs:
        pth = os.path.join(OUT_DIR, f"{slug}-backtest.csv")
        if os.path.exists(pth):
            with open(pth, newline="", encoding="utf-8") as f:
                all_rows += [r for r in csv.DictReader(f) if "p30_cells" in r]
    def calib(rows_):
        out = {}
        n_all = sum(float(r["n_all"]) for r in rows_); n_new = sum(float(r["n_new"]) for r in rows_)
        for name, lo, hi in PROB_BINS:
            cells = sum(float(r[f"{name}_cells"]) for r in rows_); hits = sum(float(r[f"{name}_hitcells"]) for r in rows_)
            sump = sum(float(r[f"{name}_sump"]) for r in rows_)
            ra = sum(float(r[f"{name}_recall_all"]) * float(r["n_all"]) for r in rows_ if r[f"{name}_recall_all"] not in ("", "nan"))
            rn = sum(float(r[f"{name}_recall_new"]) * float(r["n_new"]) for r in rows_ if r[f"{name}_recall_new"] not in ("", "nan"))
            out[name] = {"lo": lo, "cells": int(cells), "predicted": round(sump / cells, 4) if cells else None,
                         "observed": round(hits / cells, 4) if cells else None,
                         "recall_all": round(ra / n_all, 3) if n_all else None, "recall_new": round(rn / n_new, 3) if n_new else None}
        for v in ("new_rank", "persist", "habitat_only"):
            vals = [(float(r[f"{v}_new"]), float(r["n_new"])) for r in rows_ if r.get(f"{v}_new") not in (None, "", "nan")]
            nn = sum(w for _, w in vals)
            out[f"{v}_new"] = round(sum(x * w for x, w in vals) / nn, 3) if nn else None
        b = [float(r["brier"]) for r in rows_]; b0 = [float(r["brier_zero"]) for r in rows_]
        out["brier"] = round(sum(b) / len(b), 6) if b else None
        out["brier_zero"] = round(sum(b0) / len(b0), 6) if b0 else None
        st = sum(float(r["stay_sites"]) for r in rows_); sa = sum(float(r["stay_again"]) for r in rows_)
        out["stay_apparent"] = round(sa / st, 3) if st else None
        out["n_all"] = int(n_all); out["n_new"] = int(n_new); out["stay_sites"] = int(st)
        out["days"] = len(rows_); out["species"] = len({r["species"] for r in rows_})
        return out
    # combined map: chance of any rare-species report per cell, per day
    comb = None
    if combined_days:
        land = grid["land"]; bins = {n: {"cells": 0, "hits": 0, "sump": 0.0} for n, _, _ in PROB_BINS}
        bsum = b0sum = 0.0
        for d, v in combined_days.items():
            prob = 1 - np.exp(-v["lam"]); prob[~land] = 0.0
            for n, lo, hi in PROB_BINS:
                m = land & (prob >= lo) & (prob < hi)
                bins[n]["cells"] += int(m.sum()); bins[n]["hits"] += int((m & v["hit"]).sum()); bins[n]["sump"] += float(prob[m].sum())
            bsum += float(np.mean((prob[land] - v["hit"][land]) ** 2)); b0sum += float(np.mean(v["hit"][land] ** 2))
        comb = {n: {"lo": lo, "cells": b["cells"], "predicted": round(b["sump"] / b["cells"], 4) if b["cells"] else None,
                    "observed": round(b["hits"] / b["cells"], 4) if b["cells"] else None} for (n, lo, _), b in zip(PROB_BINS, bins.values())}
        comb["brier"] = round(bsum / len(combined_days), 6); comb["brier_zero"] = round(b0sum / len(combined_days), 6)
        hits_new = n_new_tot = 0
        for v in combined_days.values():
            if v["new_cells"] and v["lam_new"][land].max() > 0:
                mk = top_fraction_mask(v["lam_new"], land, TOP_Q)
                hits_new += sum(1 for c in v["new_cells"] if mk[c]); n_new_tot += len(v["new_cells"])
        comb["new_rank_new"] = round(hits_new / n_new_tot, 3) if n_new_tot else None
        comb["n_new"] = int(n_new_tot)
        comb["days"] = len(combined_days); comb["species"] = len(slugs)
        comb["n_all"] = int(sum(int(v["hit"][land].sum()) for v in combined_days.values()))
        print(f"\nCombined new-site ranking: top 5% of cells caught {100 * (comb['new_rank_new'] or 0):.0f}% of {comb['n_new']} new sites")
        print("Combined map (any rare species): " + "  ".join(
            f"{n} n={comb[n]['cells']} {100*(comb[n]['predicted'] or 0):4.1f}%->{100*(comb[n]['observed'] or 0):4.1f}%" for n, _, _ in PROB_BINS[:4])
            + f"  Brier {comb['brier']} vs zero {comb['brier_zero']}")
    calibration = {"generated_at": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z", "combined": comb,
                   "note": "In-sample over the archived days; pooled per method group.",
                   "all": calib(all_rows) if all_rows else None,
                   "guilds": {g: calib([r for r in all_rows if r["guild"] == g]) for g in GUILDS if any(r["guild"] == g for r in all_rows)}}
    with open(os.path.join(OUT_DIR, "calibration.json"), "w", encoding="utf-8") as f:
        json.dump(calibration, f, ensure_ascii=False, indent=1)
    print("\nReliability (pooled): class -> cells, predicted chance vs observed share with a report, share of next-day reports:")
    for g, c in list(calibration["guilds"].items()) + [("ALL", calibration["all"])]:
        if not c: continue
        print(f"  {(GUILDS[g]['label'] if g in GUILDS else g):28} " + "  ".join(
            f"{n} n={c[n]['cells']} {100*(c[n]['predicted'] or 0):4.1f}%->{100*(c[n]['observed'] or 0):4.1f}% ({100*(c[n]['recall_all'] or 0):3.0f}% of reports)"
            for n, _, _ in PROB_BINS[:4]) + f"  Brier {c['brier']} vs zero {c['brier_zero']}")

    print("\nPooled top-5 percent hit rate per method group (all / new sites), weighted by target reports:")
    print(f"{'group':26} {'spp':>3} {'n_all':>5} {'n_new':>5} | " + " ".join(f"{v:>10}" for v in variants))
    for g in list(GUILDS) + ["ALL"]:
        rows = summary if g == "ALL" else [s for s in summary if s["guild"] == g]
        if not rows:
            continue
        print(f"{(GUILDS[g]['label'] if g != 'ALL' else 'ALL SPECIES'):26} {len(rows):3d} {sum(r['n_all'] for r in rows):5d} {sum(r['n_new'] for r in rows):5d} | "
              + " ".join(f"{f_(pooled(rows, v, 'all'))}/{f_(pooled(rows, v, 'new'))}" for v in variants))
    print("written:", path)


if __name__ == "__main__":
    main()
