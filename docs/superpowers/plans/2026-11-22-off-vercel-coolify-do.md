# Off Vercel — Coolify on DigitalOcean: implementation plan

**Status:** not started · **Spec:** [`../specs/2026-11-22-off-vercel-coolify-do-design.md`](../specs/2026-11-22-off-vercel-coolify-do-design.md)
**Written:** 2026-11-23T01:55Z, after re-probing every system named below.
**Corrected:** 2026-11-23 — factual fixes from plan review (hub `/health` + Node 24, office orchestrator cron,
real Astro ISR/`/preview/[id]`, openhand Clerk + `pnpm sync`, bridge URL cutover, deferred NAS tunnel work).

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps are `- [ ]` checkboxes; when a step
> actually runs, mark it `- [x] ✅ <UTC>` and paste the command output that proves it. This plan spans reboots and
> four machines, so the checkbox log **is** the state. Never mark a step done on the strength of intent.

## Goal

Move four workloads — `airlie-hub`, `lhr` (Astro + marketing homepage), `lhr-office`, `openhand` (`apps/web`) — off
Vercel Hobby onto one DigitalOcean Droplet running Coolify, reached through the Cloudflare tunnels and Access
policies that already exist. `ecff-website` and `lhr-mcp-server` stay on Vercel deliberately. Postgres stays where it
is (Supabase for these four apps). Droplet disk is disposable; Vercel is deleted last.

## Where things actually run (verified 2026-11-23T01:5Z from this Mac)

Re-run these before trusting the table; they take seconds.

| Fact | Value | Command |
|---|---|---|
| Vercel account | `weyenks-projects` (CLI 65.5.0) | `vercel whoami` |
| Vercel production projects | `airlie-hub` → `airlie-hub.vercel.app` (Node 24.x); `project-y1zt3` → `openhand-nine.vercel.app` (Node 24.x); `lhr` → **`lhr-nine.vercel.app`** (Node 26.x); `lhr-office` → `lhr-office-uggj0hebp-weyenks-projects.vercel.app` (Node 24.x); `ecff-website`; `lhr-mcp-server` | `vercel ls` |
| Hub source | `~/git/airlie/hub` (package under the airlie repo) — Express 5 + `pg`, engines Node `24.x`, `build: tsc -p tsconfig.build.json`, `start: node dist/index.js`. `src/index.ts:12-16` listens **only when `process.env.VERCEL` is unset**, on `PORT ?? "8787"`, **no host arg** (Node default bind — not loopback; `HOST` is unread today). Health is **`GET /health`** (`createApp.ts:36-38`), not `/api/health`. `api/index.ts` re-exports the app default (Vercel form). **No Dockerfile.** Deps: express, pg only — no Vercel Blob, no Clerk, no SIGTERM handler. | read `hub/package.json`, `hub/src/index.ts`, `hub/src/createApp.ts` |
| Office source | `~/git/lhr/apps/lhr-office` (workspace name `lhr-office`, engines `26.x`) — Express + `@lhr/db` (`pg`); `src/server.ts` exports `createApp(db)`; `api/index.ts` does `export default createApp(db)` and **never listens**; local listen lives only in `scripts/dev.ts` (`PORT ?? 3001`). Bundle: esbuild (`scripts/bundle.mjs`). Cron: **`/api/cron/orchestrator`** (`vercel.json` `0 13 * * *`, bearer `CRON_SECRET`). Health: **`GET /health`**. Tests: single script `vitest run`. Bridge env: `AIRLIE_HUB_URL` + `AIRLIE_HUB_TENANT_KEY` (`src/bridge/config.ts`). | read those files |
| Astro site source | `~/git/lhr` root — Astro 5 + **`@astrojs/vercel`** with `isr: { expiration: 300, bypassToken: process.env.ISR_BYPASS_TOKEN, exclude: ['/preview/[id]'] }` (`astro.config.mjs:8-14`). Draft preview is `/preview/[id]`, not `/api/preview`. | read `astro.config.mjs` |
| openhand source | `~/git/openhand` — pnpm + turbo: `apps/{api,mobile,web}`, `infra/Dockerfile.api` exists (**no web Dockerfile**; base `node:25-bookworm-slim`); `apps/web` is Next.js (`prebuild: pnpm sync`, `build: next build`, `start: next start --port 3001`) with **no `output: 'standalone'`**. Still depends on **`@clerk/nextjs`**. | read `apps/web/package.json`, `next.config.ts` |
| Database | Hub `pg`; `@lhr/db` (`packages/db/src/client.ts`) is a `pg` Pool over **`DATABASE_URL`** with Supabase pooler semantics (`ssl: { rejectUnauthorized: false }`, pool-mode comment). Office uses the same. → these four apps need **no DB move**, only env carry-over. | read `client.ts` |
| This Mac's tools | present: `doctl` (account *My Team*, droplet limit 10, **0 SSH keys uploaded**), `tailscale`, `gh`, `vercel`, `node 26.5.0` (nvm), `pnpm`; **absent: `cloudflared`, `wrangler`, 1Password CLI (`op`), `docker`** | `command -v …`, `doctl account get` |
| Tailnet (this Mac 100.96.196.123) | `HomeNet` 100.85.74.36 (NAS), `MacStudio` 100.93.122.125, `tangled-up-wireless-lan` 100.116.97.2, `Robins-Wife-iPhone` | `tailscale status` |
| SSH aliases (`~/.ssh/config`) | `homenet` → `weyenk@192.168.4.79` (fnOS NAS; `docker` needs root and `sudo -n` is refused → NAS docker work is **operator-run**); `studio` → `weyenkruswicki@192.168.4.33` (hostname `MacStudio`, node 26 + pnpm, and the only host found with `/usr/local/bin/cloudflared`); `air` → `weyenk@100.93.34.15` (host key not yet accepted). **No `droplet` alias yet.** | `grep -A4 '^Host ' ~/.ssh/config` |
| Nothing here is the Droplet | no DO droplets exist (`doctl compute droplet list` is empty), no Docker on this Mac | verified |

## Corrections to the spec's inventory table

1. **Droplet size slug:** the spec says 4 GB / 2 vCPU ≈ $24 → `s-2vcpu-4gb` ($24/mo, 80 GB). `s-4vcpu-8gb` is $48/mo.
   Task 3 creates `s-2vcpu-4gb`; the *image build jobs* run in CI, so builder RAM never matters.
2. **`lhr-office` is deployed to Vercel production today** (`vercel ls` shows a Ready production deployment), so its
   `vercel.json` cron (`/api/cron/orchestrator`, daily 13:00 UTC) may already be firing. Task 1 checks the Vercel
   cron log **before** any in-process scheduler is enabled — otherwise due jobs run twice.
3. The Astro site's production URL is `lhr-nine.vercel.app` and the Vercel account is `weyenks-projects`, not a
   "kruse-group" team.
4. Port assumptions: hub listens on **8787** by default; office `scripts/dev.ts` defaults to **3001**; openhand web
   on **3001**. Coolify service ports must match the container's real bind, not a guessed 3000.
5. Spec open note claiming office needs `prisma migrate deploy` is **wrong** — office uses `@lhr/db` / `pg` only.
   Spec Done-when "five apps" / topology drawing `lhr-mcp` on the Droplet also disagree with the deliberate
   "mcp + ecff stay on Vercel" decision this plan follows (four apps move).

## Still unknown — Task 1 resolves these; do not guess them later

- **Which host serves each tunnel's ingress.** `cloudflared` is not on this Mac and there is no `~/.cloudflared` on the
  NAS; `studio` has the binary. Task 1 finds the running service (or a container) and dumps
  `sudo cloudflared --config /usr/local/etc/cloudflared/config.yml ingress rule <tunnel>` plus the tunnel id and public
  hostname for every app hostname. Until that is known, no cutover step can be written concretely.
- **The Coolify panel URL and admin credentials.** The spec says one already runs on the LAN; it is not in this Mac's
  tailnet list. Task 1 records its URL, version and whether the DO Droplet will be a *new* Coolify instance
  (expected) or that box relaunched.
- **Cloudflare API access for DNS.** `cloudflared` on this Mac is absent, so either an API token exists (Task 1 looks
  in the iCloud secrets folder) or DNS edits are dashboard-only (**operator-run**).
- **Live env keys per Vercel project** (names, not values) — `vercel env ls production <project>`.

## Global rules

- **Secrets.** This Mac has no `op`. The store in use is the iCloud folder both repos already read from:
  `~/Library/Mobile Documents/com~apple~CloudDocs/lhr-secrets/<repo>/.env.local` (mode 600). New infra env files go
  there as `infrastructure/<service>.env`. Values are pasted into Coolify/Vercel/DO by hand; **never** into a commit,
  a shell history-friendly position in CI logs, or this plan.
- **Two apps at a time on a hostname:** the previous origin keeps listening until the cutover has soaked (spec
  "Per-cutover procedure"). Rollback = re-point that one hostname, never a global rollback.
- **operator-run** steps need the NAS password, `sudo` on another host, or a dashboard. Mark them
  `⟪operator: <host>⟫` and wait — do not fake the output.
- **Ports / TLS path (pick once in Task 1, then stick to it):** Coolify UI is **tailnet-only** (never public 8000).
  App traffic is either (A) existing Cloudflare tunnels → Droplet tailnet IP:container-port, or (B) Cloudflare-proxied
  DNS A/AAAA → Droplet public **80/443** (Caddy/Coolify proxy). Task 4 opens ufw 80/443 so (B) is possible; if Task 1
  records that all four hostnames stay on tunnels (A), those ports stay unused from the public internet and cutovers
  only edit tunnel ingress. Do not mix paths per hostname without writing it in the inventory.
- **Verification loop, every step:** make the change → `curl --resolve <host>:443:<droplet-ip> -sS https://<host><path>`
  or the service's own health endpoint → `curl -sS https://<real-hostname><path>` through Cloudflare → paste output in
  the checkbox line.
- **Commits:** `~/git/lhr`, `~/git/airlie`, `~/git/openhand` are separate remotes; one branch per repo
  (`prod-deploy-do`), and the new `~/git/infrastructure` repo holds all Droplet/Coolify/tunnel configuration so every
  change is reviewable.

## Task map

| # | Task | Outward-facing change |
|---|---|---|
| 0 | Preconditions, tooling, secrets audit | none |
| 1 | Authoritative inventory (tunnels, Coolify, Vercel env names) | none |
| 2 | `~/git/infrastructure` skeleton + preflight | new repo |
| 3 | Droplet created + hardened | DigitalOcean |
| 4 | Coolify installed, GitHub + GHCR wired, tailscaled on Droplet | Droplet |
| 5 | Tailnet reachability (tunnel → Droplet, Droplet → NAS if needed) | tailnet policy ⟪operator⟫ |
| 6 | `hub` image + CI push to GHCR | lhr/airlie repos, GHCR |
| 7 | `lhr-office` gains a real entrypoint + image | lhr repo, GHCR |
| 8 | `openhand` web image (standalone) | openhand repo, GHCR |
| 9 | Astro off `@astrojs/vercel` (ISR replacement) + image | lhr repo, GHCR |
| 10 | Staging on the Droplet: all four behind `staging.*` + Access | Droplet, Cloudflare |
| 11 | Orchestrator scheduler consolidated in office (Vercel cron removed after cutover) | lhr repo |
| 12 | Cutover: `airlie-hub` + point bridge/office at new origin | Cloudflare, airlie + lhr config |
| 13 | Cutover: `web.lhr.life` (Astro site) | Cloudflare |
| 14 | Cutover: `lhr-office` then `openhand.help` | Cloudflare |
| 15 | Pull-based monitoring (NAS Prometheus) + orchestrator health signal | NAS ⟪operator⟫ |
| 16 | Backups + Droplet rebuild drill | NAS |
| 17 | Decommission: pause the four Vercel projects, docs (NAS tunnel retirement = optional follow-up) | Vercel, Cloudflare |

## Phase 0 — Ground truth before anything changes

### Task 0: Preconditions, tooling, secrets audit (no outward change)

**Files:** `~/git/infrastructure/.secrets-note.md` (does not hold values — it holds *locations*).

- [ ] `brew install cloudflared jq` (this Mac has neither; both are needed for Tasks 1 and 12-14).
      Verify: `cloudflared --version && jq --version`.
- [ ] Confirm identities still valid, and record the *account* names only:
      `doctl account get | head -3`; `gh api user -q .login`; `vercel whoami`.
- [ ] `ls -l ~/Library/Mobile\ Documents/com~apple~CloudDocs/lhr-secrets/` and note which repos have `.env.local`.
      Any app whose env lives **only** in Vercel gets flagged here — that flag is what Task 1's env dump exists to clear.
- [ ] `git -C ~/git/lhr status --short && git -C ~/git/airlie status --short && git -C ~/git/openhand status --short`
      — every repo must be clean or the dirty files named before Task 6 starts branching.

**Commit:** nothing yet.

### Task 1: Authoritative inventory ⟫ writes the file every later task cites

**Files:** `~/git/infrastructure/inventory/2026-11-23-baseline.md`.

- [ ] **Vercel side, per project** (the four names from `vercel project ls`):
      `vercel env ls production <project> | awk '{print $1}'` → key **names** only, into the inventory under each app.
      Also `vercel project inspect <project>` for build command / output dir / framework preset — those become the
      Dockerfile's `CMD`/`ENV`, so they must be transcribed, not remembered.
- [ ] **Orchestrator cron evidence:** `vercel ls lhr-office | tail -5`, then in the Vercel cron log
      ⟪operator: vercel.com ⟫ record whether `/api/cron/orchestrator` (`vercel.json` schedule `0 13 * * *`)
      has fired in the last 7 days.
- [ ] **Who serves the tunnels.** On each tailnet host that owns the `lhr.life` zone traffic:
      `sudo launchctl list | grep -i cloudflared; ps aux | grep -i [c]loudflared; sudo ls /etc/cloudflared /usr/local/etc/cloudflared 2>/dev/null`
      (this Mac: nothing; NAS: no `~/.cloudflared`; check `studio` first, then the NAS container list).
      Record: host, config path, and `tunnel --config <path> route list` output (hostname → service) for
      `airlie-hub`, `web.lhr.life`, `openhand.help`, and the office/staging hostnames the spec wants added.
- [ ] **Cloudflare auth for DNS:** try `cloudflared tunnel list` after sourcing a token from the secrets folder
      (`CLOUDFLARE_API_TOKEN`); if no token exists, write "DNS = dashboard only" in the inventory and mark every DNS
      step ⟪operator⟫.
- [ ] **Coolify:** get the existing panel URL/version (Task 1 asks: `docker ps --filter name=coolify` on the box that
      has it, or the LAN address Noah supplies ⟪operator⟫). Record whether the Droplet gets a *fresh* panel (expected,
      per spec: "Droplet becomes the only box") and what happens to whatever runs on the LAN box today.
- [ ] **Data-plane decisions:** for each app, note DB target (`DATABASE_URL` host: Supabase vs NAS Postgres) from the
      env names captured above — hub and office are `pg` against Supabase (verified in source); confirm the Astro site's
      usage before Task 9 assumes it.
- [ ] **Public hostnames in DNS:** `dig +short` for each hostname from the tunnel inventory (`web.lhr.life`,
      hub, office, `openhand.help`, staging names) — paste answers into the inventory.
- [ ] Commit: `git add inventory && git commit -m "inventory: 2026-11-23 baseline"`

**Done when:** every later task can cite `inventory/2026-11-23-baseline.md` instead of a guess. If any box is still
empty, stop and ask — do not fill it in from memory.

### Task 2: `~/git/infrastructure` skeleton + preflight (local only)

**Files:**
- `~/git/infrastructure/script/lib.sh` — ssh targets, logging, and a guard that refuses to run against `prod` unless
  `--yes` is passed.
- `~/git/infrastructure/script/preflight.sh` — every CLI the plan needs, each with an explicit *degraded* line instead
  of a crash.
- `~/git/infrastructure/{cloudflared,caddy,coolify,backups,monitoring}/README.md` — one paragraph each saying what
  lives there and where the live copy is (this is what makes the repo the source of truth).
- `~/git/infrastructure/.gitignore` — `.secrets/`, `*.env`, `inventory/*-secrets.md`.

- [ ] Write the four files. `preflight.sh` checks: `doctl compute droplet list` reachable; `ssh nas true`,
      `ssh studio true`; `tailscale status --peers=false`; `docker` **absent is OK and prints**
      `MISSING docker — images are built in GitHub Actions (Task 6+), not here`; `op` may be missing (recorded in
      Global rules).
- [ ] `cd ~/git/infrastructure && git init -b main && gh repo create infrastructure --private --source=. --push`
      (verify the created repo name/visibility in the output).
- [ ] `script/preflight.sh` → paste output into the checkbox line.

**Commit:** `infrastructure: skeleton, preflight, guardrails`.

## Phase 1 — The Droplet ("dobby") exists and is the only new box

### Task 3: Create the Droplet

**Files:** `~/git/infrastructure/droplet/create.sh` (idempotent: refuses if a droplet named `dobby` already exists).

- [ ] Key pair + upload:
      `ssh-keygen -t ed25519 -f ~/.ssh/dobby_do -N '' -C "dobby-$(date +%F)"` then
      `doctl compute ssh-key import dobby --public-key-file ~/.ssh/dobby_do.pub`.
      Verify: `doctl compute ssh-key list | grep dobby`.
- [ ] Size before spending: `doctl compute size list | awk '$1 ~ /^s-(2|4)vcpu/ {print}'` — confirm `s-2vcpu-4gb` is
      $24/mo and note the price in the checkbox. The spec's "s-4vcpu-8gb" was a slug error ($48); builds happen in CI.
- [ ] `doctl compute droplet create dobby --image ubuntu-24-04-x64 --size s-2vcpu-4gb --region ams3 \
      --ssh-keys <id-from-previous-step> --enable-monitoring --backups --tag-name lhr-prod`
      Record the droplet id + public IP in `inventory/2026-11-23-baseline.md`.
      Region: `ams3` is Amsterdam; if Noah wants London, use `lon1` — decide once here and write it down.
- [ ] Wait for active: `until doctl compute droplet get dobby -f Status | grep -q active; do sleep 10; done`
- [ ] `ssh-keyscan -H <IP> >> ~/.ssh/known_hosts` and add a `Host dobby` block to `~/.ssh/config`
      (`HostName <IP>`, `User root`, `IdentityFile ~/.ssh/dobby_do`). Verify `ssh dobby 'echo $(hostname) $(lsb_release -rs)'`.
- [ ] Commit `create.sh` immediately — this is the script that recreates the box if the disk dies.

**Done when:** `doctl compute droplet get dobby` shows active + backups enabled, and `ssh dobby uname -a` works as root
with the key only.

### Task 4: Harden, then Tailnet **before** any service or container

Doing tailscale first is why the Postgres-only-on-LAN rule survives (spec §5).

**Files:** `~/git/infrastructure/droplet/bootstrap.sh` (runs over ssh, idempotent).

- [ ] `bootstrap.sh`: create `noahkrus` with sudo, `PermitRootLogin no`, `PasswordAuthentication no`, install ufw
      (`allow OpenSSH`; also `80, 443` so TLS path B from Global rules remains possible — Coolify **8000** stays
      denied on public), fail2ban for the coolify ssh user too (docker.sock is root-equivalent), unattended-upgrades,
      and `/swapfile` (2G) — Coolify image pulls benefit even though app builds run in CI.
      Re-verify ssh **before** closing the door: keep one session open while a second `ssh dobby` works as the new user.
- [ ] `⟪operator: Tailscale admin⟫` create a reusable auth key (exponential expiry off, tag `lhr-prod`).
      Then `bootstrap.sh` part 2: install tailscale, `tailscale up --authkey=$TS_KEY --hostname=dobby --ssh`.
- [ ] `⟪operator: Tailscale admin⟫` review the ACL so `tag:lhr-prod` may reach the NAS on **5432 only** (and
      9100/node-exporter from the Droplet), and nothing else. Do not enable SSH-for-Tailscale to the NAS.
- [ ] Prove both directions: `ssh dobby 'tailscale ip -4'` → from the NAS
      `⟪operator: nas ⟫ sudo ufw status numbered` (fnOS manages the firewall — record what actually gates 5432), and
      `pg_isready -h <nas-tailscale-ip> -p 5432` from the Droplet.
      If Postgres still binds LAN-only, the fix is a **tailscale serve / socat stunnel on the Droplet side**, not
      `listen_addresses = '*'` — write whichever was used into the inventory.
- [ ] Pin the tailnet IP in `~/.ssh/config` (`Host dobby-tail`) so later tasks do not depend on the public IP.
- [ ] Commit.

**Done when:** `tailscale ping` works both ways and Postgres answers **from the Droplet over the tailnet** while
`nc -zvw2 <droplet-public-ip> 5432` still fails from this Mac.

### Task 5: Install Coolify on the Droplet (checksum first, then run)

- [ ] Download and hash before executing anything:
      `curl -fsSL -o /tmp/coolify-install.sh https://cdn.coollabs.io/coolify/install.sh && shasum -a 256 /tmp/coolify-install.sh`
      Record the hash; fetch the published checksum for the release being installed ⟫operator: web⟫ and compare.
      Only on a match: `scp /tmp/coolify-install.sh dobby:/root/ && ssh dobby 'bash /root/coolify-install.sh'`.
- [ ] `⟪operator: browser ⟫` finish setup at `http://<dobby-tailnet-ip>:8000` (never expose 8000 publicly; ufw stays
      closed on it). Create the admin user, then **Settings → Private key**: generate a key pair in Coolify and add the
      public half to `dobby:~/.ssh/authorized_keys` (the spec's flow — no secret leaves the box).
- [ ] Add localhost as a Coolify resource: `ssh dobby 'test -S /var/run/docker.sock && echo socket-present'`.
- [ ] Record in the inventory: panel version (`docker inspect coolify --format '{{.Config.Image}}'`), admin URL, and
      that 8000 is tailnet-only.
- [ ] Commit `droplet/coolify-install.md` with the exact steps + hashes.

**Done when:** panel reachable over the tailnet, self-hosted instance listed, and `ssh dobby 'docker ps'` shows
coolify + its postgres + redis + realtime containers healthy.

## Phase 2 — Images, built in CI (this Mac has no Docker; that is fine)

Every app task here follows the same shape, because openhand already proved it: Dockerfile in-repo, GitHub Actions
builds and pushes to GHCR with `github_sha` + rolling tags, Coolify pulls by tag and restarts. **No image is ever built
on the Droplet, and none is hand-pushed** (spec §4). Public base images are pinned by digest after the first successful
pull — `docker image inspect --format '{{index .RepoDigests 0}}'` on the Droplet, then put that digest in the
`FROM` line.

Each app also needs its **staging** deploy key, because staging on the Droplet uses `pull_policy: always` (that is what
the "local staging" finding means — it is not Vercel's staging).

### Task 6: `hub` — image + CI, deployed to staging

**Files:**
- Create `~/git/airlie/hub/Dockerfile`
- Create `~/git/airlie/.github/workflows/push-hub-image.yml` (openhand's `push-staging-image.yml` is the reference;
  hub is npm not pnpm, so the install step differs)
- Create `~/git/airlie/hub/.dockerignore` (`node_modules`, `dist`, `.vercel`, `tests`)
- Modify `~/git/airlie/hub/src/index.ts` only if you add an explicit host + SIGTERM (optional but recommended for Coolify)

- [ ] Read the sources of truth before writing: `hub/package.json` (`build`, `start`, `engines: "24.x"`),
      `hub/src/index.ts` (port default **8787**, Vercel listen guard; **no host arg today**),
      `hub/src/createApp.ts` (**`GET /health`** → `{ ok: true }`). Transcribe into the inventory.
- [ ] Optional but recommended before the image: make listen honor `HOST` (default `0.0.0.0` in containers) and
      handle `SIGTERM` → `server.close()` + `pool.end()`. Today neither exists — do not claim "same as hub" elsewhere.
- [ ] `Dockerfile`: build stage `FROM node:24-alpine AS build` (must match `engines.node` **24.x**, not 26),
      `COPY hub/` (or build context = `hub/`), `npm ci`, `npm run build`;
      runtime stage `FROM node:24-alpine`, non-root `USER node`, copy `dist/` + production `node_modules` +
      `package.json`, `ENV PORT=8787 HOST=0.0.0.0`, `EXPOSE 8787`,
      healthcheck via Node (alpine may lack curl/wget):  
      `HEALTHCHECK --interval=30s --timeout=3s CMD node -e "fetch('http://127.0.0.1:8787/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"`,
      `CMD ["node", "dist/index.js"]`.
- [ ] Workflow: `on: push: branches: [main, prod-deploy-do], paths: ['hub/**', …]`;
      `docker/login-action` with `GITHUB_TOKEN` to `ghcr.io`; build+push
      `ghcr.io/${{ github.repository_owner }}/airlie-hub` tagged `${{ github.sha }}` and `staging`; digest-pin
      after the first green run.
- [ ] Push branch `prod-deploy-do`, watch `gh run watch`. **Verify the image, not just the build:**
      `ssh dobby 'docker pull ghcr.io/<owner>/airlie-hub:staging && docker run --rm -p 8799:8787 -e DATABASE_URL=... ghcr.io/<owner>/airlie-hub:staging &'`
      then `curl -s localhost:8799/health`. Paste the JSON (`{"ok":true}`). (Real `DATABASE_URL` from the secrets
      folder; a crash-on-boot fails this step.)
- [ ] Coolify: create the `hub` service on the **staging** project pointing at that GHCR tag, env from
      inventory key names ⟫operator: coolify⟫ (`DATABASE_URL`, `BRIDGE_WORKER_SECRET`, optional
      `TENANT_SERVICE_KEYS` / `ALLOWED_KINDS`), health check **`/health`**, restart `unless-stopped`.
- [ ] Commit on `prod-deploy-do` (hub stays on Vercel until Task 12 — nothing outward changes here).

**Done when:** CI pushes `:staging`, and the container answers **`/health`** on the Droplet over its tailnet IP.

### Task 7: `lhr-office` — make it runnable outside Vercel, then image it

Today production has **no long-lived listen**: `api/index.ts` is `export default createApp(db)` for Vercel, and the
only listen is `scripts/dev.ts`. The cron that must keep working is **`/api/cron/orchestrator`** (not a daily-brief).

**Files:**
- Create `~/git/lhr/apps/lhr-office/scripts/prod-server.ts` (or sibling) — production entry, modeled on `scripts/dev.ts`
- Modify `~/git/lhr/apps/lhr-office/package.json` (add `start` pointing at the bundled/prod entry)
- Create `~/git/lhr/apps/lhr-office/Dockerfile` + `.dockerignore`
- Create `~/git/lhr/.github/workflows/push-office-image.yml`

- [ ] Read `api/index.ts` and `src/server.ts`: app is **`createApp(db)`** from `src/server.ts`; Vercel handler is
      that Express app as default export. The new entry **imports `createApp` + `getPool`** the same way — do not
      re-mount routers.
- [ ] Prod entry (mirror `scripts/dev.ts`): require `DATABASE_URL` + `SUPABASE_JWT_SECRET` at boot (exit 1 if missing);
      `reapOrphanedRunningRuns`; `const port = Number(process.env.PORT ?? 3001)`;
      `const server = createApp(db).listen(port, '0.0.0.0', …)` with a startup log; add **new** SIGTERM handling
      (`server.close()`, pool end) — hub does not have this yet, so write it here deliberately.
- [ ] Keep `api/index.ts` and `vercel.json` cron working **until Task 14** — this task only *adds* a container entry.
- [ ] Verify locally without Docker: `npm run build -w lhr-office` (workspace name is **`lhr-office`**, not
      `@lhr/office`) then start the new target on `PORT=3123` and
      `curl -s localhost:3123/health` → `{"status":"ok"}`. Also hit one real route behind auth (e.g. `GET /api/jobs`
      without a token → 401). `npm run test -w lhr-office` (vitest) must still pass.
- [ ] `Dockerfile`: build context = `~/git/lhr` monorepo root; install workspaces; build deps
      `@lhr/db`, `@lhr/jobs`, `@lhr/llm` then office (`npm run build -w lhr-office` / equivalent). Runtime:
      Node **26** (`engines.node`), `ENV PORT=3001`, healthcheck **`/health`** via `node -e` + `fetch`,
      `CMD` = the new prod entry (or `node dist/…` after bundle). No Prisma — there is none.
- [ ] CI pushes `ghcr.io/<owner>/lhr-office:{sha,staging}` on `apps/lhr-office/**` + `packages/**`.
- [ ] Coolify staging: env from inventory. Bridge calls need `AIRLIE_HUB_URL` + `AIRLIE_HUB_TENANT_KEY`
      (`src/bridge/config.ts` throws if either is missing when bridge routes run). Cron path needs `CRON_SECRET`.
      Do **not** invent `DAILY_BRIEF_*` keys.
- [ ] Commit.

**Done when:** staging office serves `/health` and one authenticated API route on the Droplet, and the Vercel copy is
still green.

### Task 8: `openhand` web — standalone image + static copy

**Files:**
- Create `~/git/openhand/infra/Dockerfile.web` (`infra/Dockerfile.api` is the working reference — `node:25-bookworm-slim`, pnpm)
- Modify `~/git/openhand/apps/web/next.config.ts` — set `output: 'standalone'` (it is **not** set today)
- Create `~/git/openhand/.github/workflows/push-web-image.yml` (copy `push-staging-image.yml`, change filter/tag)

- [ ] Read `apps/web/next.config.ts` + `apps/web/package.json` and record: `prebuild: pnpm sync`,
      `build: next build`, `start: next start --port 3001`, **no standalone yet**, Clerk still in deps
      (`@clerk/nextjs`). Transcribe into the inventory.
- [ ] Add `output: 'standalone'` to `next.config.ts`. Build context must include whatever `pnpm sync` /
      `scripts/sync-assets.mjs` + `sync-docs.mjs` need (those scripts, not a nonexistent
      `prebuild:generate-theme.ts`). If a prior staging failure blamed a missing sibling package, re-run the
      failing build and record the real missing path in the inventory before guessing — do not invent an api
      token-validation step that is not in `package.json`.
- [ ] Runtime stage copies `.next/standalone` **and separately** `.next/static` → `apps/web/.next/static` and
      `public/` → `apps/web/public`. Missing either yields 200 HTML with 404 chunks — verify assets:
      `curl -sI https://staging.openhand.help/<a-real-_next-static-asset>` → 200 + JS content-type.
- [ ] Port **3001**, `HOSTNAME=0.0.0.0` in the image (matches `start --port 3001`). Prefer bookworm-slim like
      `Dockerfile.api`, not alpine/wget.
- [ ] CI → `ghcr.io/<owner>/openhand-web:{sha,staging}`; Coolify staging with Clerk + app env from inventory
      ⟫operator⟫ (Clerk keys still required — Clerk is **not** removed from this app).
- [ ] Commit.

**Done when:** staging web returns 200 at `/` **and** static chunks load, and `/` or the sign-in surface shows the
**Clerk** flow (current product auth — do not expect a credentials-only form).

### Task 9: Astro site off `@astrojs/vercel`

The site is the only app that **loses a platform feature**. Today `astro.config.mjs` uses:

```js
adapter: vercel({
  isr: {
    expiration: 300,
    bypassToken: process.env.ISR_BYPASS_TOKEN,
    exclude: ['/preview/[id]'],
  },
})
```

Node standalone has no ISR that survives a container restart — record the cost here, not at 1am.

- [ ] **Decision (spec's choice):** `@astrojs/node` in `standalone` mode; pages that can stay static keep
      `export const prerender = true`; pages that relied on ISR revalidation render per request. Draft preview stays
      the **`/preview/[id]`** route (`src/pages/preview/[id].astro`); `ISR_BYPASS_TOKEN` / office `revalidate.ts`
      callers need a documented replacement (front cache or drop revalidate calls) — note which in the commit.
      Cost in the commit message: non-prerendered pages render in-process. If slow later, add Caddy cache in a
      follow-up — do not bring back `@astrojs/vercel`.
- [ ] `npm remove @astrojs/vercel && npm install @astrojs/node` (adapter dep as the Astro docs for the version require);
      edit `astro.config.mjs`: swap adapter, **delete the `isr` block**, keep whatever `site` / `trailingSlash` exist
      (re-read the file — do not invent keys that are absent).
- [ ] **Build-time env:** record which Supabase (or other) keys a failing `npm run build` actually demands; put those
      in CI secrets. Do not guess key names from memory.
- [ ] `npm run build` locally **must pass** before CI. Then start the standalone entry (`PORT=4321 node …` — use the
      path `@astrojs/node` standalone actually emits for this Astro version) and `curl -s localhost:4321/` plus
      `/preview/…` shape check. Absolute asset URLs still pointing at `*.vercel.app` are a Task 13 checklist item.
- [ ] Add `.dockerignore` (`dist`, `node_modules`, `.astro`, `.vercel`) and a Dockerfile: build with secrets, runtime
      copies `dist/` + production deps, Node major from root `engines` (**26.x**).
- [ ] CI → `ghcr.io/<owner>/lhr-site:{sha,staging}`. **No scheduler in this image** — office's orchestrator cron
      stays on Vercel until Tasks 11 + 14.
- [ ] Commit on `prod-deploy-do` in `~/git/lhr`.

**Done when:** `npm run build` is clean, the standalone server serves `/` (and a docs or content page) locally, and CI
has pushed a `:staging` image.

## Phase 3 — Staging, then one hostname at a time

### Task 10: Staging on the Droplet, gated by Cloudflare Access

**Files:** `~/git/infrastructure/cloudflared/ingress.md` (paste-back blocks per tunnel, live host recorded in Task 1).

- [ ] Coolify: one **staging** environment (already exists on the old LAN box; recreate it here so the new box is
      self-contained) and point each of the four services at their `:staging` tag with `pull_policy: always`.
- [ ] DNS ⟫operator⟫: one A record `staging.lhr.life → <dobby public IP>` (proxied is fine and preferred, so the
      origin needs no public listener besides 80/443). Caddy on the Droplet serves
      `staging.airlie-hub.<host-from-Task-1>`, `staging.web.lhr.life`, `staging.lhr-office.<host>`,
      `staging.openhand.help` — a wildcard `*.staging.lhr.life` is simpler if you prefer it; pick one and write it in
      `caddy/Caddyfile`.
- [ ] Cloudflare Access ⟫operator⟫: one self-hosted application covering the staging hostnames, policy = allow your
      email only. Verify from this Mac with `curl -sI` (expect a redirect to `dash.cloudflare.com/...access`), which is
      also the proof required in spec §7.
- [ ] All four staging services restarted once and re-checked (hub **`/health`**, office **`/health`**, Astro pages,
      openhand `/` + a static chunk). Commit.

**Done when:** the four staging URLs respond through Access, each returning the container's own health body, and
`ssh dobby 'docker ps'` shows no restart loop.

### Task 11: Orchestrator — move the clock out of Vercel without double-running

Production today: `vercel.json` cron `0 13 * * *` → **`/api/cron/orchestrator`**, handled in `src/server.ts` via
`handleCron` → `runDueJob(db, registry)` with bearer `CRON_SECRET`. There is **no** `daily-brief` route. Staging and
production will exist **at the same time**, so an unguarded in-process scheduler double-fires due jobs.

**Files:**
- Keep cron handlers in `~/git/lhr/apps/lhr-office/src/server.ts` (already the single source)
- Modify prod entry from Task 7 to optionally start an in-process scheduler
- Modify `/health` payload in `src/server.ts`
- Add/extend tests under `~/git/lhr/apps/lhr-office/tests/` (vitest; `server.test.ts` already covers the cron route)

- [ ] Read `runDueJob` / orchestrator persistence and list in the commit body: how a job marks itself due/done, and
      whether a second trigger in the same window is already a no-op. If a double fire can run the same job twice,
      add/confirm the guard in the orchestrator path (not a fictional brief dedupe table).
- [ ] Scheduler in the **prod entry** (not Vercel): fire on the same cadence as today (**13:00 UTC** unless inventory
      says otherwise) via `node-cron` or `setInterval`+time check, gated by
      `ENABLE_ORCHESTRATOR_SCHEDULER === 'true'`. Staging leaves it unset; production sets it only at Task 14 after
      soak. Invoking the scheduler must call the same `runDueJob` path as `handleCron` (shared function — no second
      copy of job selection).
- [ ] `/health` grows `lastOrchestratorIso` + `lastOrchestratorError` (or equivalent) for Task 15 alerts.
- [ ] Tests: cron route still 401 without `CRON_SECRET` / succeeds with it (`server.test.ts` patterns); scheduler does
      **not** start when the flag is off; with the flag on, one tick calls `runDueJob` once.  
      `npm run test -w lhr-office`. Local smoke: flag off → no tick logs; flag on against a DB with nothing due →
      safe no-op outcome in logs.
- [ ] Commit on `prod-deploy-do`. Nothing outward: production still Vercel cron only.

**Done when:** scheduler-off is tested, staging `/health` exposes orchestrator fields after a manual/flagged run, and
Vercel cron log still shows its normal single daily fire.

## Phase 4 — Cutover, one hostname per change

Rules for every task in this phase: previous origin stays listening; exactly one `public_hostname` line (or DNS
record, if Task 1 chose path B) changes; verify through Cloudflare with `curl --resolve`; then walk each app's **own**
auth surface (hub = tenant/worker bearer keys; office = Supabase Auth; openhand = Clerk; Astro preview = signed
preview token). Token refresh against a new origin is the failure `curl /health` will not catch.

### Task 12: `airlie-hub` first

Everything else calls the hub, so it moves before its consumers — then **point those consumers at the new origin**.

- [ ] Coolify: production service on the immutable sha tag just verified; production hostname from Task 1; turn **off**
      "Generate localhost" for it.
- [ ] Re-point per Task 1's TLS path: tunnel ingress → `http://<dobby-tailnet-ip>:8787`, **or** DNS/Caddy to the
      published port. Timestamped config copy; `cloudflared tunnel ingress validate` before any tunnel restart.
- [ ] From this Mac: `curl -sS https://<hub-host>/health` → container body `{"ok":true}`, and
      `curl -sI https://web.lhr.life` still the pre-cutover origin (proves nothing else moved).
- [ ] **Consumer config (required by the design):**
      - airlie: set `bridge.job_bus_url` to `https://<hub-host>/api/v1` in the live config (`airlie.local.yaml` /
        deploy host — see `src/airlie/config/schema.py`; unset raises). Restart/reload the bridge so it uses it.
      - office Coolify (staging + prep production env): `AIRLIE_HUB_URL` (+ `AIRLIE_HUB_TENANT_KEY`) to the new
        origin. A competitor/bridge call that hits the hub proves the wire.
- [ ] Hub auth smoke: one tenant-authenticated API call and one worker claim/heartbeat against the new origin; watch
      logs for auth failures (hub has **no** Clerk).
- [ ] Record hostname, old/new ingress (or DNS), timestamps. Commit `infrastructure` (+ config diffs in airlie/lhr as
      needed, **no secrets in git**).

**Done when:** the hub hostname is served by the Droplet, `bridge.job_bus_url` and office `AIRLIE_HUB_URL` point at it,
a real consumer call succeeds, and the Vercel hub deployment remains as rollback.

### Task 13: `web.lhr.life` — Astro site

Hub does **not** serve a marketing `/` today (no `hub/src/routes/marketing.ts`); this cutover is Astro-only.

- [ ] Coolify service for `lhr-site` with production domain `web.lhr.life`; confirm bind port matches the published
      port before touching DNS/tunnel.
- [ ] Re-point `web.lhr.life` per Task 1's TLS path (same copy/validate/restart discipline).
- [ ] Verify what actually breaks: `/` (fix any remaining `*.vercel.app` absolute asset URLs — leave the old Vercel
      app up as asset origin or rewrite to this origin), a content/docs page that previously relied on ISR (edited
      recently → new copy shows without waiting on a CDN ISR clock), **`/preview/[id]`** with a valid signed preview
      token (not `/api/preview`), and trailing-slash behaviour if configured (match today's redirects).
- [ ] Commit `infrastructure` + any URL fixes in `lhr`. **Do not** delete nonexistent hub marketing routes.

**Done when:** `web.lhr.life` is Astro on the Droplet, draft preview still works, hub cutover from Task 12 still green.

### Task 14: `lhr-office`, then `openhand.help`

Same procedure, two hostnames, not one commit.

- [ ] Office: re-point → verify `/health` + Supabase sign-in + one real action (e.g. jobs/candidates UI) + one
      orchestrator tick evidence (`lastOrchestratorIso` or cron-compatible manual `GET /api/cron/orchestrator` with
      `CRON_SECRET`). **Then** set `ENABLE_ORCHESTRATOR_SCHEDULER=true` in Coolify production, confirm the next due
      window runs **once** (Vercel cron still present until the next step — prefer disabling Vercel cron the same day
      after the first good in-process run, or pause the Vercel project only after scheduler proof). Remove the `crons`
      block from `apps/lhr-office/vercel.json` once the in-process scheduler is proven so a Vercel redeploy cannot
      double-fire.
- [ ] openhand: re-point `openhand.help` → verify `/` + static chunks from the new origin, **Clerk** sign-in, one
      protected action. Postgres/InstantDB stay as today; missing `.next/static` is still the usual 200-with-broken-JS
      failure — check assets before suspecting auth.
- [ ] Between the two, `curl -sI https://openhand.help` to prove office's change did not disturb it if they share a
      tunnel restart.
- [ ] Commit `infrastructure` with both ingress/DNS diffs.

**Done when:** all four hostnames are on the Droplet, each with a logged auth walkthrough, orchestrator is not
double-firing, and no Vercel project is deleted yet.

## Phase 5 — Monitoring, backups, decommission

### Task 15: Pull-based monitoring (NAS Prometheus), plus the orchestrator health signal

- [ ] `⟪operator: NAS ⟫` confirm the Prometheus instance and where its `prometheus.yml` lives, then add a scrape job for
      the Droplet over Tailscale. Record which exporters need to exist and make them real Coolify services on the
      Droplet (`node-exporter`, and cAdvisor if you want per-container CPU) — bound to the **tailscale interface only**,
      never public `0.0.0.0`.
- [ ] Alert rules (existing system only — no second alert stack): `up == 0` per Droplet target for 5m; one alert on
      office `/health` orchestrator fields from Task 11 (`lastOrchestratorIso` older than ~36h, or
      `lastOrchestratorError` non-empty — metric or `blackbox_exporter` JSON probe; match existing convention). Also
      blackbox HTTPS probes for the four public hostnames (spec).
- [ ] Keep Vercel-era uptime checks for what stays on Vercel (`ecff-website` / `www.ecff.club`, `lhr-mcp-server`) and
      for NAS apps the Droplet does not front.
- [ ] Verify by deleting one target deliberately, watching the alert fire, then restoring it. Commit
      `infrastructure/monitoring/`.

### Task 16: Backups you have actually restored

- [ ] DO snapshot policy: `⟪operator: cloud.digitalocean.com ⟫` weekly Droplet snapshot, retained 4 weeks. Verify with
      `doctl compute droplet get dobby` (or the inventory's recorded check) rather than trusting the toggle.
- [ ] App data lives in Supabase, which is off our hook — write that plainly in `backups/README.md` instead of implying
      we back it up. What **is** ours: Coolify's own Postgres (its config/env), which Coolify can back up on a schedule;
      enable that and confirm a dump exists on disk. `ssh dobby 'ls -la /data/coolify/backups 2>/dev/null || true'`.
- [ ] **Rebuild drill** (the spec's "test it by rebuilding the Droplet"): create a throwaway Droplet from the latest
      snapshot, run the recorded bootstrap script, and confirm the four services come back from images + Coolify env
      without your hands. Destroy the throwaway immediately after; record timings in
      `~/Library/Mobile\ Documents/com~apple~CloudDocs/secrets/deploy-credentials.md` under a `recovery` heading.

### Task 17: Decommission, then the close-out ritual

Only after every app has soaked ≥1 day behind the Droplet.

- [ ] Vercel ⟫operator⟫: pause `airlie-hub`, `lhr`, `lhr-office`, `openhand` (dashboard, not `vercel rm`). Keep
      `ecff-website` and `lhr-mcp-server`. Note: pausing stops Hobby crons — Task 14 must already have proven the
      in-process orchestrator.
- [ ] Re-point nothing back; verify each app twice more over the next two days, then delete the four Vercel projects
      and remove dead `vercel.json` / adapter leftovers from the moved apps' repos.
- [ ] Old LAN Coolify: delete *duplicated apps* only (`⟪operator⟫`); the box itself stays ("delete apps, not boxes").
- [ ] Docs: append `## Outcome` to the design spec (each hostname → what serves it); update
      `~/git/infrastructure/README.md` (`Where everything lives`); set this plan's status to done.
- [ ] Close-out: pending commits in affected repos; `git status` clean where you expected clean.

**Out of scope for this plan (optional follow-up):** retiring NAS `cloudflared` routes for apps that never left the
NAS (`pool`, `homelab`, …) in favor of LAN Caddy. Do not block decommission of the four Vercel apps on that work.

---

## Self-review notes (author, 2026-11-23T02:0Z; corrected 2026-11-23 after plan review)

- Spec coverage: §1 Droplet ✓ T3-5; §2 images/CI ✓ T6-9; §3 per-app cutover ✓ T10-14 (ecff and `lhr-mcp-server`
  deliberately excluded); §4 monitoring ✓ T15; §5 backups/rebuild ✓ T16; §6 decommission/docs ✓ T17.
  Bridge URL cutover (`bridge.job_bus_url`, `AIRLIE_HUB_URL`) is explicit in T12. Orchestrator re-home matches spec
  cutover step 5 (not a nonexistent daily-brief).
- **Corrections applied after review (do not reintroduce):**
  - Hub health **`/health`**; engines/image **Node 24**; listen has no host/`HOST` today; no Clerk; no marketing route.
  - Office cron **`/api/cron/orchestrator`**; workspace **`lhr-office`**; vitest; bridge env
    `AIRLIE_HUB_URL` + `AIRLIE_HUB_TENANT_KEY`; no Prisma; no `DAILY_BRIEF_*`.
  - Astro ISR shape matches `astro.config.mjs` (`isr` + `/preview/[id]` + `ISR_BYPASS_TOKEN`).
  - openhand still uses **Clerk**; `prebuild` is `pnpm sync`; add `output: 'standalone'`.
  - TLS path A vs B recorded once; Coolify UI tailnet-only; NAS tunnel retirement deferred.
- Mid-flight revision risks: ISR → per-request cost (T9); staging hostname scheme (T10); orchestrator double-fire
  window between enabling `ENABLE_ORCHESTRATOR_SCHEDULER` and removing Vercel cron (T14).




