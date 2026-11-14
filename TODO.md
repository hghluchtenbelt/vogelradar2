# TODO

Infra/migration state lives in private notes outside this repository
(the repo is public; server addresses and tunnel IDs do not belong in
it); this is the loose-ends list.

- [x] **Vogelradar: cluster repeat sightings of the same rarity**
  (website done on dev/staging 2026-07-23; app port pending): a
  staked-out rare bird gets reported by many observers over several
  days, so one individual shows up as a pile of markers and cards.
  Rare/very-rare markers currently bypass the marker-cluster layer on
  purpose (always visible), so generic clustering is not the fix.
  Proposed approach, client-side only (no server/API change): group
  sightings of the *same species* within ~2 km of each other, show
  only the most recent one as marker + card with a "3x gemeld" badge,
  and list the older reports inside the popup/card on tap. 2 km
  roughly matches "same site" (same lake/polder); the 7-day window
  makes same-species-within-2km almost certainly the same bird.
  Needs the same change in vogelradar.html and www/index.html
  (remember: the two files differ on purpose, port by hand).
- [x] **Vogelradar: drop sightings that were deleted on waarneming.nl**
  (done on dev 2026-07-23 as the daily revalidation sweep in
  updater.py: runs after the first scrape once 24h have passed,
  deletes 404/410 rows, syncs corrections and backfills photo URLs;
  force with `python updater.py --revalidate`; login-protected
  observations are left untouched):
  false reports get removed from the site later, but the scraper only
  ever inserts, so they linger locally for up to 7 visible days.
  Proposed approach: a revalidation sweep in updater.py, one run per
  day (e.g. the first run after 03:00), that re-fetches the
  observation URL of every stored sighting still inside the visible
  window and deletes rows that return 404/410 or redirect away.
  Cost is small: one authenticated session (Anubis PoW already
  solved for the scrape) plus roughly 200-600 requests at the
  existing 0.35 s politeness delay, a few minutes of mostly-idle
  network time, negligible CPU/RAM. Alternative if that feels heavy:
  a round-robin batch (~30 oldest-checked URLs per hourly run, needs
  a checked_at column) spreads the same work across the day.
  Also covers the case where the observation was corrected rather
  than deleted (species/coords edited): while re-fetching we can
  re-parse and update the row instead of only checking liveness.
- [x] **Vogelradar: show the observation photo on tap**
  (website done on dev/staging 2026-07-23; app port pending): scrape the
  photo/thumbnail URL in _scrape_observation (we already detect
  photo presence) and expose it in birds.json; the frontend shows
  the image when the user taps a sighting. Server side benefits
  website and app at once; the UI part is a frontend change, so it
  reaches app users only with the next Play Store release. Check
  hotlinking politeness first (or proxy/cache thumbnails ourselves).
- [ ] **Vogelradar: move the scraper out of the web process**: the
  hourly scrape (and daily sweep) run in a daemon thread inside the
  API container, so every deploy resets the one-hour timer, scrape
  memory peaks linger in the web process (Python keeps the heap), a
  hung scrape can affect the API, and multiple uvicorn workers would
  each start their own scraper. Plan: a Coolify Scheduled Task on the
  same app runs `python updater.py` hourly (same image, no extra
  infra), and SCRAPE_INTERVAL_MIN=0 disables the in-process thread
  (small api.py change). Test on staging first; statusbot needs no
  change (it reads scrape_runs from the DB via the API).
- [x] **Vogelradar: highlight sightings new since last visit**
  (website done on dev 2026-11-14; app port pending): the newest
  observation number seen is kept in localStorage; on the next visit
  (more than 30 minutes later) anything with a higher number gets a
  "nieuw" label on the card, a ring on the marker and a count in the
  header. Pure client-side, no server change.
- [ ] **Vogelradar: species mini-history**: tap a rare or very rare
  species to see its recent reports (locations, "al X dagen ter
  plaatse"). Scoped to zeldzaam/zeer zeldzaam only. Builds directly
  on the repeat-sighting clustering above, which already groups per
  species; do it in the same slice or right after.
- [ ] **Vogelradar: offline snapshot**: today the app shows an empty
  list when there is no connectivity (the birds.json fetch fails
  silently and state.data stays empty). Cache the last successful
  birds.json response client-side (localStorage or Cache API) and
  render it with an "offline, laatst bijgewerkt om HH:MM" notice.
  Zero server cost; it is purely on-device caching.

All frontend items above must be applied to both vogelradar.html and
www/index.html (ported by hand, the files differ on purpose) and only
reach app users after a new versionCode + Play Store upload.

- [ ] **Vogelradar app: port the November 2026 website work**
  (website on dev since 2026-11-14): loading and error states, manual
  location (municipality search or a tap on the map) with the "no
  location known" notice, radius filter, "nieuw" since last visit,
  share link in the popup, and the about sheet with source credit.
  Not needed in the app: preview tags, manifest, back-button handling.
  Already in the app release branch `review-fixes`: Capacitor 8.6.2,
  the privacy fixes and subspecies-aware wish matching.
- [ ] **Vogelradar: landing pages per species and gemeente** (idea
  from the November 2026 review, for search traffic). Deliberately not
  built yet: it would republish scraped waarneming.nl data as
  indexable pages, which is a different relationship with the source
  than a map of recent sightings. Decide that first.
- [ ] **Vogelradar: traffic numbers**: the site is proxied by
  Cloudflare, whose zone analytics already show requests and unique
  visitors per day without any code or privacy-page change. Check
  there first; a self-hosted counter only if app versus website needs
  to be split.

- [ ] **Natwacht: own KNMI API key**: add `KNMI_API_KEY` to the
  natwacht-api app via the Coolify UI (deploy.hermen.dev, project
  natwacht, Environment Variables) and restart. Runs on KNMI's
  anonymous fallback key until then.
- [x] **Natwacht: design exploration**: done 2026-07-21, several
  artifact rounds (style directions, UI concepts, night-watch
  variants, data-first layouts). Outcome LIVE on natwacht.nl: the
  "Dubbelwacht" (umber/gold single dark theme, EB Garamond, chart and
  radar coupled via one gold time cursor, stat row, lantern signal in
  the top bar, minimal text). The earlier Buienmaatje restyle was
  merged the same day and then superseded; both are in main history.
- [ ] **Natwacht: rain alarm (must-have)**: local notification before
  rain starts, based on the current location tracked in the
  background (Android WorkManager via Capacitor, no server-side
  location). Needs a design/plan first.
- [ ] **Natwacht: home screen widget (later)**: headline + mini rain
  timeline as an Android widget; requires native widget code outside
  the webview.
- [ ] **R2 offsite backups**: deferred until a bigger-than-hobby
  project needs it; steps are in the private infra notes.
