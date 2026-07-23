# TODO

Infra/migration state lives in NEW-VPS-COOLIFY.md; this is the loose-ends list.

- [ ] **Vogelradar: cluster repeat sightings of the same rarity**: a
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
- [ ] **Vogelradar: drop sightings that were deleted on waarneming.nl**:
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
- [ ] **Vogelradar: show the observation photo on tap**: scrape the
  photo/thumbnail URL in _scrape_observation (we already detect
  photo presence) and expose it in birds.json; the frontend shows
  the image when the user taps a sighting. Server side benefits
  website and app at once; the UI part is a frontend change, so it
  reaches app users only with the next Play Store release. Check
  hotlinking politeness first (or proxy/cache thumbnails ourselves).
- [ ] **Vogelradar: highlight sightings new since last visit**:
  remember the newest seen sighting ids in localStorage and mark
  anything newer with a "nieuw" accent on card + marker. Pure
  client-side, no server change.
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
  project needs it; steps are in NEW-VPS-COOLIFY.md.
