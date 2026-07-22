# TODO

Infra/migration state lives in NEW-VPS-COOLIFY.md; this is the loose-ends list.

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
