# Forecast test: "activity now" map per species

Experimental tab (`/voorspelling`) that ranks 1 km cells in the Netherlands by
expected activity of one rare species for the next day, for every species
with rarity "zeldzaam" or "zeer zeldzaam" in the archived data (about 90 in
late October 2026). Species are assigned to method groups (`guilds.py`:
irruptives, territorial winter visitors, migrant passerines, waterbirds,
geese, raptors, seabirds, waders, residents) that supply the parameters; a
few well-studied species carry overrides (`SPECIES_OVERRIDES` in
`model.py`). Background and rationale: `reports/Rare bird forecast feasibility.md`.

## Running inside the web app (integration with the scrape)

`forecast/pipeline.py` rebuilds every layer from the live database and is
called from the updater thread in `api.py` after each scrape when
`FORECAST_ENABLED=1`. It reads the last 7 days of rare rows through
`database.get_sightings`, writes the layers to `FORECAST_OUT_DIR` (use a
folder on the `/data` volume, the image directory is not writable) and keeps
a daily archive of the rare rows in `FORECAST_DATA_DIR/archive/` for later
backtests. Wind is refreshed at most every 6 hours (16 Open-Meteo calls).
The whole run takes a few seconds for about 450 species (84 with reports on
24 October 2026) and needs only numpy and Pillow, which are now in
`requirements.txt`. The API serves the results at `/forecast/index.json`,
`/forecast/<slug>.json` and `/forecast/<slug>.png`, so the website and the
app can use the same endpoints. Test on staging first (`FORECAST_ENABLED=1`,
`FORECAST_OUT_DIR=/data/forecast/out`, `FORECAST_DATA_DIR=/data/forecast`).

The species universe is the waarneming.nl export in `soortenlijst/`: every
"zeldzaam" or "zeer zeldzaam" species (449 layers; subspecies of rare
species are merged into the species, rare subspecies of common species keep
their own layer), plus species the feed itself marks rare (source "feed").
Species without reports are listed with zero sites and no map.

## Layout

| Path | What |
|---|---|
| `model.py` | Builds the ranked grid per species and as-of date; writes `out/<slug>.png`, `out/<slug>.json`, `out/index.json` |
| `pipeline.py` | Container entry point: database to layers after each scrape, daily archive, stale-file cleanup |
| `stay.py` | Empirical stay model (logistic regression on archived site-days) and the new-site rate |
| `species_list.py` | The species universe from the waarneming.nl export |
| `guilds.py` | Method groups with parameters, Dutch labels and method text; name-to-group rules and aliases |
| `wind.py` | Open-Meteo 10 m wind on a 4 x 4 grid (cached in `data/wind/`), night and day means, interpolation |
| `evaluate.py` | Rolling-origin backtest; writes `out/<slug>-backtest.csv` (summary is picked up by `model.py` into the JSON) |
| `archive_feed.py` | Saves a gzipped snapshot of the public feed to `data/snapshots/`; run daily, production prunes after 15 days |
| `data/landcover_1km.npz` | ESA WorldCover 2021 class fractions per ~1 km cell (300 x 273, lat 50.7 to 53.7, lon 3.2 to 7.3), built from the 10 m tiles |
| `data/snapshots/*.json.gz` | Feed snapshots (input) |
| `data/prod-export-*.json.gz` | Optional exports of the production `sightings` table (same loader, `bird_name` rows) |
| `voorspelling.html` | The tab (Leaflet image overlay on the CARTO basemap) |
| `out/` | Generated layers, served by `api.py` under `/forecast/<file>` |

## Model (version 3: chance of a report per cell)

```
lambda(cell) = sum_sites p_stay(site) * spread(cell | site)  +  n_new * density_new(cell)
P(cell)      = 1 - exp(-lambda(cell))
```

* `p_stay(site)`: logistic regression (`stay.py`) fitted on every archived
  site-day (site known on day D, outcome: a report of the species within
  3 km on D+1) with features method group, log days present, age of the last
  report (today / yesterday / older), photo, log reports, flock. Refit by the
  pipeline on each run; first fit (24 October 2026, 2,095 site-days): base
  rate 19.5 percent, Brier 0.124 against 0.157 for the base rate. Observed
  rates: 1 day present 14 percent re-reported, 2 days 26, 3 to 4 days 47,
  5 or more 50; last report today 32, yesterday 12, older 8; with photo 28,
  without 12; seabirds 2, raptors 5, waterbirds 24.
* `spread`: 0.6 of the mass in a 0.25 km Gaussian and 0.4 in a 1.2 km one
  (re-reports: median 0.18 km, 75th percentile 0.95 km from the site),
  normalised over the cells within 5 km.
* `n_new`: expected new sites tomorrow, 0.6 x new sites per day over the last
  3 days + 0.4 x the 7-day mean. `density_new`: the "moved" kernel (per-group
  scale, shifted by the night wind for drifting groups) times habitat, coast
  and onshore wind, normalised to one over Dutch land cells.
* Legend classes are absolute and the same for every species: 30 percent or
  more, 10 to 30, 3 to 10, 1 to 3; below 1 percent the cell stays blank.
  "Tomorrow" is the day after the latest archived day for every species, so
  a bird last reported days ago gets a low chance, not a stale map.

Two layers per map. The brown layer is the chance above; in practice it
colours only the surroundings of known sites, because the chance of a new
site per 1 km cell is below 1 percent everywhere (even per 10 km block the
maximum was about 5 percent on 24 October 2026), while 37 percent of
next-day reports in the backtest were at new sites. The green layer
(`<slug>-nieuw.png`, `alle-nieuw.png`) therefore shows the new-site part as
a ranking, not a chance: the 1, 5, 10 and 25 percent of cells with the
highest expected density of new sites (moved kernel x habitat x coast x
wind, times the expected number of new sites). The backtest reports the
share of new sites caught by its top 5 percent (`new_rank_new` in
`calibration.json`, per group and for the combined map) next to the
persistence baseline; the legend shows both.

The combined map (`out/alle.png`, `out/alle.json`, slug `alle`, the page's
default) sums lambda over every species: the chance of at least one report
of any rare or very rare species per cell, with the three species that
contribute most to each cell at or above 1 percent (shown on tap) and every
recent site with its species. Its reliability is checked in the backtest as
`calibration.combined`.

Earlier versions (per-species percentile ranking of a kernel times habitat)
are kept for comparison as `score_rank` and the `rank_v2` backtest variant.
Lesson from the ranking versions: habitat multiplied the "stayed" mass too,
which ranked a forest above the village where a Nutcracker had been
reported 21 times; steering habitat only through the "moved" part scored
worse, so the probability model keeps habitat in the new-site density only
and gives known sites their fitted chance directly.

The backtest (`evaluate.py`) reports reliability per class (predicted mean
against the observed share of cells with a report), Brier score against
"always zero", and the top-5 percent hit rates for all reports and new
sites; the stay model used on day D is fitted on site-days before D once
there are at least 150 of them (earlier days use the full fit and are
flagged in the CSV).

## Run

```
.venv/bin/pip install -r forecast/requirements-forecast.txt
.venv/bin/python forecast/archive_feed.py            # daily
.venv/bin/python forecast/evaluate.py --species all --quiet   # add --full for all variants
.venv/bin/python forecast/model.py --species all
uvicorn api:app --reload                             # open /voorspelling?soort=<slug>
```

`out/index.json` lists the species (with group and backtest summary) and
the groups; the page builds its selector and its "Soorten" tab from it.
Wind is fetched on demand (16 Open-Meteo calls) and cached per day in
`data/wind/` (gitignored, like the snapshots).

Run `evaluate.py` before `model.py` so the JSON carries the latest backtest
summary. The newest snapshot day is partial (hourly feed), so the last
backtest row underestimates every variant.

## Known limits

* No history beyond the archived snapshots: GBIF's copy of waarneming.nl lags
  two months and the 2018 Nutcracker records there are one staked-out bird in
  Wageningen (about 2,900 reports in two 5 km cells).
* Sightings follow observers; the effort toggle is a crude correction.
* Licensing: waarneming.nl data via Vogelradar's scrape, non-commercial;
  WorldCover CC BY 4.0. Only aggregated cells are published.
