# New VPS (Coolify) setup: state and next steps

## Next session: start here

State as of 2026-07-21 (afternoon session): the Cloudflare MCP
re-auth fixed the write scope. Steps 2 and 3 of the old plan are DONE:

- Access service token `coolify-mcp` created (id
  `3460744b-b4f4-461a-b186-bf5b56ff89a0`, expires 2027-07-21).
  Service Auth policy `coolify-mcp-service-token` (non_identity,
  precedence 2) added to the "Coolify" Access app
  (`dd66823c-baec-4162-8e56-a241dfd27253`, deploy.hermen.dev); the
  existing "alleen-ik" allow policy is untouched. The two
  `--header` args are in the coolify entry of `~/.claude.json`.
  Verified via curl: with the token headers, Coolify itself answers
  (`{"message":"Unauthenticated."}`, its own API-token layer).
- ALL traffic now goes through the tunnel: tunnel config v2 has
  ingress `hermen.dev` and `*.hermen.dev` -> https://localhost:443
  with noTLSVerify true, catch-all 404. Both DNS records were
  converted in place (PUT, atomic) to proxied CNAMEs ->
  `601cee57-b9bc-470d-a943-bb4f6f0cd9ab.cfargotunnel.com`.
  Verified: all sites 200 (status/deploy 302 to Access, expected),
  `cloudflared_tunnel_total_requests` counts up on fetches.
- Reminder: LE HTTP-01 renewals by Traefik may stop working now that
  traffic is tunneled; harmless (browsers see CF edge certs,
  cloudflared skips verification via noTLSVerify).

DONE 2026-07-21 (same session): the user closed 80/443 in the TransIP
panel firewall. Verified from outside: 80, 443 and the SNI bypass to
deploy.hermen.dev all time out; SSH still works; all sites 200/302
via the tunnel. UFW allow rules for 80/443 removed (only 22 left).

DONE 2026-07-21 (evening session): after the session restart the
Coolify MCP picked up the `--header` args and works end-to-end.
Verified via `get_infrastructure_overview`: 1 server (reachable),
4 projects, 3 applications, Uptime Kuma service, all running.

Remaining steps, in order:

1. Ask about the R2 bucket (step 3 of "Next steps" below) when
   convenient.

Security notes, resolved 2026-07-21: the accidentally pasted Coolify
root API token has been deleted (user confirmed). The token
`claude-monitoring` is used by the user's monitoring app/session from
their Windows environment. Possible later improvement: it is a root
token; a read-only token would suffice for monitoring.

Handoff notes, last updated 2026-07-21. Read this first when continuing
the VPS/infra work. No secrets in this file; secrets live in root-only
files on the servers.

## The two servers

| | Old VPS "cloud" | New VPS "hermen-vps" |
|---|---|---|
| SSH | `hghluchtenbelt@85.10.140.213` | `root@149.210.181.5` |
| Specs | 843 MB RAM, tight | 2 vCPU, 4 GB RAM, 96 GB disk (12% used) |
| Runs | vogelradar (vogel-radar.nl), natwacht, statusbot | Coolify 4.1.2 + Traefik v3.6, all *.hermen.dev apps |
| Plan | migrate vogelradar to Coolify later, then retire | eventually move everything to Hetzner |

## New VPS: what runs there

- Coolify at https://deploy.hermen.dev (behind Cloudflare Access).
- Apps: `hermen-dev` (hermen.dev + www, landing page),
  `tmnf-map-generator` (tmnf.hermen.dev), `tmnf-viz`
  (tmnf-viz.hermen.dev), Uptime Kuma (status.hermen.dev, behind
  Cloudflare Access, admin user `hermen`, embedded MariaDB).
- Cloudflare Tunnel `hermen-vps`: cloudflared runs as a systemd service
  on the host. Ingress (config v2): `hermen.dev` and `*.hermen.dev` ->
  `https://localhost:443` (Traefik, noTLSVerify). All traffic enters
  via the tunnel since 2026-07-21; DNS is proxied CNAMEs to
  `<tunnel-id>.cfargotunnel.com`. Unknown subdomains return Traefik's
  503 "no available server"; that is normal.
- Security already in place: UFW (22 only), TransIP panel firewall on
  (22 only; 80/443 closed 2026-07-21, origin reachable exclusively via
  the tunnel), fail2ban, unattended-upgrades, SSH key-only, Docker
  DOCKER-USER chain drops external 8000/6001/6002, Traefik dashboard
  8080 bound to 127.0.0.1 (edited /data/coolify/proxy/docker-compose.yml).
- Backups: `/usr/local/bin/coolify-backup.sh` via `/etc/cron.d/coolify-backup`
  daily 03:20 server time. Dumps Coolify Postgres + config (source/.env,
  ssh keys) to `/data/coolify/backups/instance/`, keeps 14 days,
  root-only files. rclone is installed for a future offsite sync.
- statusbot (on the OLD VPS) now also HTTP-checks hermen.dev,
  tmnf.hermen.dev and tmnf-viz.hermen.dev through the tunnel, so
  Telegram alerts cover both boxes. Source:
  `/home/hermen/Documents/projects/vps-statusbot/`.

## Next steps, in order

1. DONE 2026-07-21: **Cloudflare MCP** set up with write access.
2. DONE 2026-07-21: **apex route fixed**, and expanded: ALL traffic
   (apex + wildcard) now enters via the tunnel, see the top section.
3. **R2 offsite backups**: user creates bucket `hermen-vps-backups` +
   API token (Object Read & Write, scoped to bucket) and fills
   `/root/r2-credentials.env` on the new VPS (R2_ENDPOINT,
   R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET). Then wire rclone
   into coolify-backup.sh and test an upload.
4. DONE 2026-07-21: **origin closed**, TransIP panel firewall and UFW
   both allow only 22; verified from outside.
5. **Optional DNS hardening**: SPF record to `v=spf1 -all` and add
   `_dmarc` TXT `v=DMARC1; p=reject; sp=reject;` (domain sends no mail).
6. **Later, vogelradar migration to Coolify**: needs a plan first.
   Watch out for: SQLite file needs a persistent volume,
   firebase-service-account.json must be provided as a secret/mount,
   hourly scraper runs in a background thread of api.py, and the
   statusbot should move along (or be merged with Uptime Kuma).
   Coolify MCP: decided 2026-07-21 to set up `@masonator/coolify-mcp`
   (npm, 42 tools, supports Cloudflare Access service tokens via
   --header flags, also has Hetzner provisioning tools). User setup:
   Coolify API token (Keys & Tokens), a Cloudflare Access service
   token, a Service Auth policy on the deploy.hermen.dev Access app,
   then `claude mcp add --scope user coolify -e COOLIFY_BASE_URL=...
   -e COOLIFY_ACCESS_TOKEN=... -- npx -y @masonator/coolify-mcp@latest
   --header "CF-Access-Client-Id: ..." --header
   "CF-Access-Client-Secret: ..."`. May be mid-setup when a new
   session starts; verify with /mcp.

## Known quirks

- Cloudflare Access answers for deploy/status.hermen.dev at the edge
  even if the origin is dead; do not use those URLs as liveness checks.
- hermen.dev certs are Cloudflare edge certs (auto-renewed): no cert
  monitoring needed for that domain.
- The user's Cloudflare account had an email-verification hiccup when
  creating API tokens ("invalid"); unresolved, MCP OAuth avoids it.
