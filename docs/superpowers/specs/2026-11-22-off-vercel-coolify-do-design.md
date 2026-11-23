# Off Vercel — Coolify on DigitalOcean (design)

Date: 2026-11-22. Brainstormed with Noah the same day.

## What this is

Move all of Noah’s Vercel Hobby workloads **except ecff** onto a
**DigitalOcean Droplet** running **Coolify** (open-source git-deploy + ops GUI),
keep managed SaaS (Supabase, Clerk, R2, etc.), leave **airlie** on
**nas.home.apra**, and keep **Mac Studio** reachable via Tailscale / tunnel —
not hairpinned through the old Vercel hub or the new VPS.

## Why

- Hobby plan paused everything together; always-on APIs (especially
  **airlie-hub** and **lhr-office**) are a poor fit.
- Need a Vercel-like GUI: **git push → deploy** and an **ops console** (env,
  logs, SSL, start/stop).
- Coolify is OSS and free to self-host; cost is the Droplet, not a PaaS license.
- Start small on DO (account already exists); stay portable to another host later.

## Inventory leaving Vercel (keep ecff)

| Project | Production signal | Stack | Keep off-box |
| --- | --- | --- | --- |
| **airlie-hub** | `airlie-hub.vercel.app` | Express (`airlie/hub`) | Supabase `DATABASE_URL`, bridge secrets |
| **openhand** (`project-y1zt3`) | `openhand.help` | Next.js (`apps/web`) | Clerk; existing API / InstantDB |
| **lhr** | `lhr-nine.vercel.app` | Astro (+ Vercel adapter → drop for Node/Docker) | Postgres, Umami, ConvertKit (no Vercel Blob usage in this repo) |
| **lhr-mcp-server** | `lhr-mcp-server.vercel.app` | Express | Cloudflare R2, GitHub OAuth, Vercel Blob (OAuth token store — stays on Vercel until that app is redesigned) |
| **lhr-office** | `lhr-office-….vercel.app` | Node (`apps/lhr-office`) | Supabase, OpenRouter, cron secrets; **new hub URL** |
| **ecff-website** | `www.ecff.club` | — | **Stays on Vercel** (transfer in progress) |

## Topology

```
Internet
   │
   ▼
Cloudflare DNS  →  DigitalOcean Droplet
                      ├── Coolify (control plane)
                      ├── airlie-hub
                      ├── openhand.help
                      ├── lhr / lhr-mcp / lhr-office
                      ├── node_exporter (+ other exporters)
                      └── scraped by home Prometheus (Tailscale)

nas.home.apra     → airlie only (update-channel dogfood)
Mac Studio        → Tailscale / optional CF tunnel (not via Droplet)
ecff              → Vercel (unchanged)
```

**Portability rule:** apps are Docker images from Git; secrets live in Coolify
(plus a password-manager export); durable data stays on managed SaaS. Moving
hosts = new VM → install Coolify → re-add apps → flip DNS.

## Provider and sizing

- **Provider:** DigitalOcean (existing account).
- **Droplet:** Basic **4 GB / 2 vCPU / 80 GB** (~$24/mo). Cheaper 1–2 GB boxes
  fight Coolify + five Node apps; resize upward later if needed.
- **Region:** pick closest to users / Supabase region for hub latency; document
  the choice in the runbook.
- **Builds:** prefer **GitHub Actions → GHCR**, Coolify pulls images — avoid
  compiling Next/Astro on the Droplet (OOM risk).

## Coolify

- Install Coolify on the Droplet (official install script).
- One Coolify “server” = this Droplet initially; multi-server later if needed.
- Each app: Git repo (or GHCR image), env from Vercel export, healthcheck,
  domain, Let’s Encrypt (or Cloudflare proxy — pick one TLS path).
- Dashboard on `coolify.<domain>`; restrict access (password + Cloudflare Access
  and/or firewall / Tailscale-only).

## DNS, TLS, secrets

- Point app hostnames at the Droplet (low TTL during cutover).
- Copy env vars from each Vercel project into Coolify; do not commit secrets.
- Point airlie's bridge at the new hub origin: `bridge.job_bus_url`
  (`src/airlie/config/schema.py:464`; unset raises `bridge.job_bus_url is not set`),
  and office's hub URL via `AIRLIE_HUB_URL` (`src/bridge/config.ts:7`).
- Keep Supabase, Clerk, R2, Umami, ConvertKit as-is for v1.
- Hub env is small and verified: `DATABASE_URL` + `BRIDGE_WORKER_SECRET` required,
  `TENANT_SERVICE_KEYS` / `ALLOWED_ORIGINS` optional; it already starts outside Vercel
  (`src/index.ts` listens when `VERCEL` is unset) and pulls no Clerk/Blob/JWT deps.

## Cutover order

1. Droplet + Coolify + firewall + exporters healthy.
2. **airlie-hub** — then point airlie bridge and office at it (`bridge.job_bus_url`,
   `AIRLIE_HUB_URL`).
3. **lhr** → **lhr-office**. Office needs work first (see Open
   notes): its app is library-only and the prod entry is the Vercel handler.
   **lhr-mcp-server** is not in this cutover — it stays on Vercel (step 6).
4. **openhand** (`openhand.help`).
5. Re-home the office scheduler: `/api/cron/orchestrator` is Vercel-cron-driven
   (`lhr_vercel_app`) with no in-process scheduler, so pausing that project silently stops the
   office job engine — keep it on Vercel as cron host, or move the schedule first.
6. Soak with Vercel still up; then pause/remove Vercel projects except ecff and
   **lhr-mcp-server**, which stays on Vercel on purpose — its Blob token store and GitHub
   OAuth callback live there. Nothing in this cutover waits on it.
7. Rollback = DNS back to Vercel until soak passes.

## Monitoring — wire into existing Prometheus / Grafana

Do **not** stand up a parallel Uptime Kuma / DO-only dashboard as the primary
signal. The Droplet joins the **existing Prometheus/Grafana** stack.

**Reachability:** put the Droplet on **Tailscale** (recommended). Home (or NAS)
Prometheus scrapes private Tailscale IPs so metrics ports stay off the public
internet. If Tailscale on the Droplet is deferred, scrape only via authenticated
tunnel — never expose node_exporter publicly without auth.

**Targets on the Droplet (v1):**

| Exporter / probe | Purpose |
| --- | --- |
| **node_exporter** | CPU, RAM, disk, systemd basics |
| **cAdvisor** or Docker engine metrics (if already used at home) | container CPU/mem per Coolify app |
| **blackbox_exporter** (from the existing Prometheus host, or on-Droplet) | HTTPS probes for hub, openhand.help, lhr hostnames |
| App `/metrics` or `/health` | where an app already exposes them (hub `/health`-style); do not invent heavy instrumentation in this migration |

**Grafana:** reuse existing dashboards or add a short “DO Coolify” row: node
RAM/disk, blackbox up, per-container mem. Alerting stays in the existing
Alertmanager / Grafana contact points (disk >80%, blackbox down, hub down).

**Coolify healthchecks** remain for deploy/restart behavior; they are not a
replacement for Prometheus.

**Retention:** set Prometheus `--storage.tsdb.retention.time` and point its data
directory at a persistent volume, or the TSDB grows until it fills the Droplet disk.

**Out of scope for monitoring v1:** full APM, log shipping to Loki unless the
home stack already expects it — can follow once scrapes are green.

## Day-2 ops

- **Backups:** DO Droplet snapshots on a weekly schedule; optional Coolify backup
  to Spaces. Recovers Coolify + config, not Supabase/Clerk data or the NAS Postgres the
  apps use — that needs a nightly dump shipped off-host (R2), plus a **restore drill**
  before cutover: an untested backup is not a backup.
- **Security:** SSH keys only; firewall 22 (admin), 80/443 public; Coolify UI
  restricted; unattended upgrades.
- **Resize / migrate:** document “snapshot → larger Droplet” and “new host +
  Coolify re-add apps + DNS” in a one-page runbook.

## Non-goals

- Moving **ecff** off Vercel.
- Self-hosting Postgres/Redis on the Droplet (v1).
- Running **airlie** on the VPS (stays on NAS).
- Kubernetes / EKS.
- Replacing Clerk, Supabase, or R2.

## Done when

1. Five apps green on the Droplet with production DNS.
2. Airlie bridge / local config use the new hub URL; Studio does not depend on
   `*.vercel.app` for the hub.
3. Prometheus scrapes Droplet exporters over Tailscale; Grafana shows up/down
   for public hostnames.
4. `lhr_hub_vercel`, `jcp_server_app`, `lhr_vercel_app`, `www_ecff_club` paused or
   removed only after a soak window long enough that DNS TTL plus one full cron cycle
   has elapsed, so rollback is still available when the trigger for a revert appears.
5. Runbook exists: deploy, DNS rollback, resize, move host, scrape config.

## Open implementation notes

- **No Dockerfile exists for hub, lhr-office or lhr-mcp-server** (checked all three app dirs), so
  "repository / Dockerfile" is work, not a choice. Hub already starts outside Vercel
  (`hub/src/index.ts:12`, guarded by `process.env.VERCEL`). lhr-office does **not**:
  `createApp()` never calls `listen()` and `api/index.ts` is Vercel-only, so it needs a
  real entry plus an image that also runs the client bundle and `prisma migrate deploy`.
- Confirm exact Prometheus host (NAS vs Mac) and whether blackbox already exists
  before adding a second copy.
- Astro **@astrojs/vercel** adapter must be swapped for a Node/Docker-friendly
  adapter when containerizing **lhr**.
- Vercel Blob is only used by **lhr-mcp-server** (its OAuth token store,
  `lhr/mcp-server/src/auth/blobStore.ts`); the hub has no Blob imports. Blob is **deliberately
  retained on Vercel** alongside that app — the GitHub OAuth callback URL and the token
  store both point there today. Moving it to R2 belongs to redesigning the MCP server
  later, not to this cutover; nothing on the Droplet depends on it.
