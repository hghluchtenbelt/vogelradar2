# Migration of vogelradar, natwacht and statusbot to the Coolify VPS

Date: 2026-07-21. Status: approved by Hermen (chat session 2026-07-21).

## Goal

Move the three workloads on the old VPS (85.10.140.213, "cloud") to the
new Coolify VPS (149.210.181.5, "hermen-vps") as containers, then retire
the old VPS. All public traffic enters through the existing Cloudflare
Tunnel; the origin stays closed (SSH only).

## Decisions made

- **DNS route (option A)**: both domains move to the user's Cloudflare
  account as zones (registration stays at Hostnet/TransIP; only the
  nameservers change). Traffic goes through the tunnel like hermen.dev.
  Rejected: reopening 80/443 on the origin (undoes the security work,
  creates a second operating regime).
- **Statusbot**: migrates as a container (keeps scrape summaries and
  Telegram commands). A free external check covers whole-VPS-down
  detection; recommended form is a Cloudflare Worker cron in the user's
  account, alternative is UptimeRobot.
- **Execution strategy (approach 1)**: staged, app by app, each tested
  on a temporary hermen.dev subdomain before its DNS switch. Rejected:
  single big-bang switch night (more simultaneous failure modes).
- Order: natwacht first (already dockerized, validates the pipeline),
  then vogelradar (data + secrets), then statusbot, then retirement.

## Target state

| Hostname | App |
|---|---|
| natwacht.nl + www | Natwacht web (static Vite build, Coolify static app) |
| api.natwacht.nl | Natwacht backend (existing Dockerfile) |
| vogel-radar.nl + www | Vogelradar (FastAPI + scraper thread, one container) |
| none (internal) | Statusbot (talks to Telegram only) |

Each hostname is a proxied CNAME to the tunnel plus an ingress rule to
`https://localhost:443` with `noTLSVerify` (same pattern as hermen.dev).
The Android apps need no release: domains do not change.

## Preparation (user actions)

Add both zones in the Cloudflare dashboard (records are auto-copied;
verify the copy via the Cloudflare MCP before switching), then change
nameservers at Hostnet (vogel-radar.nl) and TransIP (natwacht.nl).
Nothing changes live: records keep pointing at the old VPS until each
app's switch moment. The stale MX record on natwacht.nl (no mail server
listens on the old VPS, verified 2026-07-21) is dropped.

## Phase 1: Natwacht

- Backend: deploy from GitHub with the existing Dockerfile. Volume for
  the rain statistics (`/srv/natwacht/data`), `KNMI_API_KEY` as a
  Coolify secret.
- Web: Coolify static build (Vite) with
  `VITE_API_BASE=https://api.natwacht.nl` at build time.
- Test on `natwacht-test.hermen.dev` and `natwacht-api-test.hermen.dev`,
  then switch DNS. `deploy/deploy.sh` becomes obsolete (git push =
  deploy).

## Phase 2: Vogelradar

- Code change first: make `DB_PATH` (database.py) and the Firebase
  service-account path (notifications.py) configurable via environment
  variables, falling back to the current paths so the old VPS is
  unaffected.
- Dockerfile; volume mounted at `/data` for the SQLite database;
  `firebase-service-account.json` as a secret file mount; the hourly
  scraper thread keeps running inside the single container (one
  replica, as today).
- Test on `vogelradar-test.hermen.dev` with a copy of the database,
  including several real scrape runs (see risks) and an FCM push test.
- Switch: stop the old service briefly, copy the database with
  `sqlite3 .backup`, switch DNS. A few minutes of downtime, done at
  night.
- Precondition: the uncommitted changes in the working tree are
  committed first; Coolify deploys from GitHub.

## Phase 3: Statusbot + external check

- Statusbot becomes a Coolify app without a public domain. Changes:
  drop systemd checks (Coolify restarts containers), read scrape stats
  from the vogelradar container's internal Docker URL, drop TLS-expiry
  checks (edge certs auto-renew), keep host disk/RAM checks via a
  read-only mount.
- External whole-VPS-down check: Cloudflare Worker cron (every 5 min,
  checks the sites, Telegram alert on failure), or UptimeRobot if the
  user prefers.

## Phase 4: Wrap-up

Keep the old VPS as a fallback for about two weeks (rollback = point
DNS back at 85.10.140.213). Then: final backup off the machine, cancel
the VPS, remove old-VPS checks from the statusbot, update
NEW-VPS-COOLIFY.md and memory.

## Risks

- **Scraper**: waarneming.nl may treat the new IP/datacenter range
  differently (Anubis or blocking). Mitigated by real scrape runs from
  the test deployment before the DNS switch.
- FCM push: same service account, expected to just work; tested
  explicitly before the switch.
- Rollback in every phase is a DNS change (minutes).

## Out of scope

R2 offsite backups (deferred by the user until a bigger project needs
it), DNS mail hardening (SPF/DMARC), the Hetzner move mentioned in
NEW-VPS-COOLIFY.md.
