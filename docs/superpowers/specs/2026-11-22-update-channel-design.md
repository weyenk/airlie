# Update channel — design (v1: identity, artifacts, awareness)

Date: 2026-11-22. Decisions made with Noah during brainstorming the same day;
adversarial review the same day; revised to match the code. Primary customer is
a **corporation**; **nas.home.apra** is the first dogfood of the same model while
the personal Mac install is decoupled.

## What this is and is not

Making it safe to move a **customer's running airlie to a newer version** in a
way that survives corporate security review. v1 ships release identity, a
publishable artifact, and an admin surface that knows what is running and what
is available. It does **not** perform the update. "Apply" in v1 means "here is
exactly what the operator (or their GitOps) does" — bump a digest in a
declarative manifest — not a button that mutates the cluster from inside the
workload.

## Primary customer and first dogfood

**Primary customer / use case:** a company running airlie on their network —
proxy and private registry mirror, GitOps (Flux / Argo / similar), maintenance
windows, change control, and a security review that rejects workloads that hold
docker sockets, cloud tokens, or the ability to rewrite their own deployment.
Kubernetes is the eventual home; compose/Portainer is acceptable as a smaller
declarative surface of the same rules.

**First dogfood (why this unblocks decoupling now):** move the personal airlie
off the Mac (`uv sync` + launchd + MLX) onto a Docker stack on **nas.home.apra**
(Portainer). After that move, `git pull && uv sync` is not an update path — the
NAS must pull a new image by digest. The NAS install uses the **same**
corporate rules (digest pin, no control-plane access, recreate not surge,
channel pointers, honesty on failed feed). It is allowed to subscribe to
`latest` so we feel every green main; customers default to `stable`.

Multi-tenancy is out of scope — one deployment runs one line. Auth already has
multiple users and roles (`Principal`, invites, passkeys, Tailscale); update
APIs hang off the existing admin check so that stays true.

## Corporate-friendly rules (non-negotiable)

These are the review criteria the design optimizes for. The NAS dogfood must
obey them too — no home-lab shortcuts that would fail a customer security
questionnaire.

1. **Declarative reconcile outside the app.** Image identity lives in git /
   the stack file; a human or bot (Renovate, Flux image policy, Portainer git
   bump) changes the digest; the platform applies it. airlie never calls
   Portainer, Kubernetes, or a docker socket.
2. **Digest is identity; tags are human labels.** Manifests pin
   `@sha256:…`. Floating tags (`latest`, mutable `stable`) are not how a
   customer deploy tracks what runs.
3. **Channel pointers are auditable artifacts**, not retagged OCI names
   (immutability policies on Harbor/ECR forbid or punish retags; caches disagree).
4. **Failed feed ≠ up to date.** A blocked egress / rate-limited registry /
   broken mirror must read as an error, never as current.
5. **Air-gap and mirror are first-class.** `pinned` and `channel_pointer`
   against an internal HTTPS base URL are v1, not afterthoughts.
6. **No in-workload restore of cluster+state.** Rollback intent is handed to
   their pipeline; restore of SQLite remains an operator action (deferred apply
   path). Corporate review rejects one process that both mutates the cluster
   and rewrites state.
7. **Shared-volume SQLite forbids surge.** Docs require `Recreate` /
   `maxSurge: 0` so two versions never write one volume.

## Constraints found in the code (verified 2026-11-22; corrected after review)

- `GET /health` is ungated and returns `status`, `roles`, `commit`, and `line`
  (`serve/app.py` ~857–859). `commit` comes from `started_commit`, set once at
  app build from `build_commit()` (`serve/app.py` ~665–668). There is no
  `version` or `image_digest` field today.
- `build_commit()` (`story/pipeline.py` ~332–346) shells out to
  `git rev-parse --short HEAD` and returns `None` when the code is not a git
  checkout **or** when `git` is missing/broken. An installed container therefore
  reports no version identity via `/health` today.
- Package version is hardcoded in three places: `pyproject.toml` (`version =`,
  currently line 3), `src/airlie/__init__.py:3` (`__version__`), and the FastAPI
  app constructor (`serve/app.py` ~665, `version="0.1.0"` — OpenAPI metadata,
  not the `/health` payload). `airlie version` prints `__version__`. Config's
  top-level `version: Literal[1]` is the **config schema** epoch, unrelated.
  v1 picks one source of truth for the **package** version.
- The release job (`.github/workflows/pipeline.yml` ~355–377) reads the version
  from `pyproject.toml` and creates a GitHub Release tagged
  `v${VERSION}+${GITHUB_SHA::7}` (one release per merged commit), attaching
  **wheels only**. No OCI image, no digest, no `release.json`.
- OCI forbids `+` in a tag, so that GitHub release tag cannot be an image tag.
  Confirm with one push during implementation.
- CI builds a container today only for tests: `.github/workflows/pipeline.yml`
  ~49–68 builds `ops/test/Containerfile` (uv 0.6.0, node 26, non-root uid 1000,
  git / procps / libatomic1 — **not** libmariadb). Sibling images:
  `ops/e2e/Containerfile`. A runtime image is a new sibling
  (`ops/release/Containerfile`), not a rewrite of the test image.
- `src/airlie/serve/web_next/` is gitignored (the `task web:build` output), so
  the built UI does not travel in git. An installed copy only serves the web app
  if the image builds it in.
- Migrations are additive and best-effort with no version table:
  `supervisor/ledger.py` ~182–187, `board/store.py` ~80–81,
  `chat/threads.py` ~123–126. No `PRAGMA user_version` under `src/airlie`.
  Forward-only is luck, not policy — a failed update can leave state an older
  build may not open.
- **Auth (real seam, not the invented one):** there is no `AdminGuard`, no
  `serve/authz.py`, no `require_admin`, no `/api/admin/*`. Requests under `/v1`
  go through principal middleware (`serve/app.py`); admin-only routes call
  `_admin(request)` in `auth/api.py` (~90–94), which requires
  `Principal.role == "admin"` (Bearer API key, admin session, or invited
  Tailscale admin). Loopback helpers in `auth/principal.py` are for bootstrap
  and Tailscale trust, **not** "any LAN client is admin." A random host on a
  LAN without credentials gets **401**, not free admin. Update routes copy
  `_admin`, under `/v1/...`.
- Config is pydantic top-level sections on `AirlieConfig`
  (`config/schema.py` ~1118+). `${VAR}` expansion exists in
  `config/load.py` (~226–235) but applies **only** to each engine's `base_url`
  and `api_key`. Secrets elsewhere use `*_env` indirection (e.g.
  `BridgeSpec.worker_secret_env`). An `update:` section follows the same
  pydantic shape; registry credentials use `*_env`, not assumed `${VAR}` or
  `SecretStr` (almost unused outside Jira).
- Ticker pattern to copy: `LineTicker` in `serve/ticker.py` — injectable clock,
  cancelled on shutdown, `row()` feeds `/health`.
- A quiescence primitive already exists: `paused(state_dir)` and the
  `line.paused` flag (`story/phase.py` ~182) — stop claiming **story** work, let
  running story jobs finish. It does **not** drain chat `/v1`, bridge claims, or
  studio jobs. AIR-55's fuller "idle" definition is a different (deferred)
  primitive for the apply path.
- SQLite stores use WAL; serve + ticker already share a volume carefully. Two
  writers from a rolling update with surge on a shared volume is a real failure
  class — documented in Appendix A (`maxSurge: 0` / `Recreate`), not solved in
  code today.

## Decisions

1. **An update is deploying a different version, not mutating the running one.**
   `uv pip install -U airlie` and `git pull && uv sync` are rejected: no
   rollback, not reproducible, not declarative, and on a launchd host the swap
   succeeds while the running process keeps the old code. The unit of update is
   a new **image digest** in the customer's declarative manifest.
2. **Reconcile is customer-owned and declarative** — industry family 1 (Argo CD,
   Flux, Fleet, Renovate; Portainer git-backed stacks as a smaller instance of
   the same idea). Customer compose/Helm/CRD pins
   `image: <registry>/airlie@sha256:…`, uses `imagePullPolicy: IfNotPresent`
   (never `Always`, which loops on ImagePullBackOff when the registry rate
   limit bites), and a pull secret for a private mirror. A human or external
   bot bumps the digest; their platform redeploys. airlie does not call that
   platform.
3. **airlie never talks to the platform control plane.** No docker socket, no
   Portainer API token, no service account token, no `kubectl` in-container.
   It records intent and reports state. This is the line corporate review
   cares about most.
4. **Real semver and a clear cadence.**
   - **Package / image tag:** `0.MINOR.PATCH`. Every green main that publishes
     an image bumps **PATCH** automatically in the release job (CI writes the
     new version into `pyproject.toml` for that release artifact only, or
     derives the tag from `0.MINOR.(run_number)` — implementation picks one
     mechanism; the published tag is always unique and ordered). **MINOR**
     bumps are manual (breaking notes / `state_migration: true`). No `+sha` in
     image tags.
   - **GitHub Release tag** for the wheel may keep build metadata if useful for
     humans; the **OCI tag** is always `v0.MINOR.PATCH` (no `+`).
   - **Digest is the identity.** Tags are for humans. The git sha lives in
     image labels (`org.opencontainers.image.revision`) and in `release.json`.
5. **Channels are pointer files, not floating image tags.**
   - `stable` — **product default** for customer installs: vetted and
     orderable; only moves on explicit promotion.
   - `latest` — every green main image; dogfood (nas.home.apra) may subscribe
     here. Subscribing a customer install to every merged commit is not a
     release channel.
   - Promotion publishes or updates `channels/stable.json` (and
     `channels/latest.json` on every release) as release assets / objects.
     Each pointer is
     `{ "version", "digest", "released_at", "release_json" }` and names an
     **immutable** `v0.MINOR.PATCH` image. We do **not** retag a mutable
     `stable` OCI tag. `latest` as an OCI tag is optional candy for
     `docker pull`; airlie's feed never treats a floating tag as identity.
6. **Honesty rule:** "up to date" / `current` is shown only when a feed
   actually answered. A failed check records source, error, and timestamp — a
   blocked corporate proxy reads as "could not reach feed (error)", never as
   current.
7. **`pinned` honesty:** a `pinned` feed that answers may show `current`
   relative to the pin, but the panel labels it `pinned @ <digest>` and never
   implies upstream `stable`/`latest` agreement. This is the air-gap / change-
   control path.
8. **AIR-55 is superseded by this design** for the update mechanism. That eval
   story still describes git-fetch + `uv sync` + launchctl self-update (the
   rejected path, and the opposite of corporate-friendly). Rewrite or retire it
   before using it as an oracle; do not implement against it.

## v1 scope

### A. Version identity that survives installation

- One source of truth for the package version (`pyproject.toml`).
  `src/airlie/__init__.py` and the FastAPI app version derive from package
  metadata (importlib) instead of repeating `0.1.0`.
- `/health` gains `version` and `image_digest`. `commit` stays for dev
  checkouts (from `build_commit()` when present).
- **How `image_digest` is obtained (decided):** bake it at image build time.
  The release job builds the image, reads the resulting digest from the
  registry/build response, and writes `/etc/airlie/image-digest` (or similar)
  via a follow-up layer / file the process can read with no network and no
  runtime socket. Labels alone cannot carry the digest of the image that
  contains them without this bake step. If the file is absent (dev checkout),
  `image_digest` is `null`.
- New `ops/release/Containerfile`, modelled on `ops/test/Containerfile`:
  python slim, uv-pinned install of release dependencies only (no dev group),
  the web bundle built into the image, non-root uid 1000,
  `CMD ["airlie", "serve"]`, OCI labels `{version, revision, created}`, plus
  the baked digest file. Non-root and minimal runtime surface are part of the
  corporate story.
- Runtime tooling to settle at implementation rather than guess: `git` if story
  flows shell out to it for customer repos (the test image installs it); `node`
  only if the hub runs in the same container. No database client library is
  needed — project dependencies contain none.
- CI release job (single transaction — wheel and image succeed together or the
  release is not published):
  1. bump / assign `0.MINOR.PATCH`;
  2. build wheel **and** `ops/release` image;
  3. push `<registry>/airlie:v0.MINOR.PATCH` (immutable tag) and record the
     digest;
  4. bake digest into the image metadata file (or one rebuild with
     `ARG DIGEST` from the first push — implementation detail; one advertised
     digest);
  5. publish GitHub Release with wheel + `release.json`;
  6. update channel pointer assets (`channels/latest.json` always;
     `channels/stable.json` only on promotion).
  Never rebuild a different tree and call it the same version.

### B. Release feed and the daily check

`release.json` (per version, immutable once published):

```json
{
  "version": "0.2.3",
  "released_at": "2026-11-22T00:00:00Z",
  "git_sha": "<full sha>",
  "image": {
    "repo": "ghcr.io/<org>/airlie",
    "tag": "v0.2.3",
    "digest": "sha256:…"
  },
  "notes": "…",
  "breaking": false,
  "state_migration": false,
  "schema_epoch": 1
}
```

`schema_epoch` is a monotonic integer airlie embeds (starts at 1). Bumping it is
required when on-disk SQLite shape changes in a way an older build cannot open.
v1 still has no migration version table inside SQLite; the epoch is advisory in
the feed and shown on the panel. A release with `state_migration: true` or a
higher `schema_epoch` than the running process reports must surface as breaking
in the UI. Promoting such a release to `stable` without notes is a release-job
check (fail the promotion step).

**Source of truth for "what is newest on this channel":** the **channel pointer**
(`channels/<name>.json`), which names a digest and a `release.json` URL/path.
Adapters:

| Adapter | Use | v1 |
| --- | --- | --- |
| `channel_pointer` | fetch `channels/<channel>.json` then that release's `release.json` — GitHub release asset **or** the customer's mirrored HTTPS base (proxy-friendly) | yes — default |
| `pinned` | air-gapped / change-controlled orgs; release set by hand after security review | yes |
| `registry_tags` | list tags only — **insufficient alone** for notes/breaking; not the channel source of truth | interface only |
| `manifest_url` | one HTTPS endpoint the customer's platform owns; controls rollout later | interface only |
| `github_releases` | OSS installs with github.com egress | interface only |

Config (pydantic `UpdateSpec` on `AirlieConfig`). Product default channel is
`stable`; dogfood overrides to `latest`:

```yaml
update:
  enabled: true
  panel: true                 # false hides the admin update UI (see §C)
  feed:
    kind: channel_pointer     # or pinned
    # Customer: often an internal mirror the proxy allows
    base_url: https://gitops.example.com/airlie-releases
    # Public default when egress allows:
    # base_url: https://github.com/<org>/airlie/releases/download
    channel: stable           # product default; nas.home.apra dogfood uses latest
    registry_token_env: AIRLIE_UPDATE_REGISTRY_TOKEN   # optional; empty = anonymous
    # pinned:                 # air-gap / locked change window
    #   version: "0.2.3"
    #   digest: "sha256:…"
  check:
    interval_s: 86400
```

No `${VAR}` under `update:` unless `_expand_engine_env` is generalized (out of
scope). Use `*_env` for secrets, matching `BridgeSpec`.

The check is a ticker-shaped loop that stores available releases, the last
successful check, and the last error. `airlie update check` on the CLI is the
developer path and the cron-friendly alternative to serve doing it (useful in
locked-down environments that prefer a scheduled Job over an in-process ticker).

### C. The panel

Routes (real API shape):

- `GET /v1/admin/update` — running release, desired release, available releases
  with notes / breaking / schema_epoch, last check plus error, drift
  classification, and the exact digest string the operator should put in their
  manifest.
- `PUT /v1/admin/update/desired` — body echoes the digest (records intent only;
  does not mutate the platform).
- `POST /v1/admin/update/check` — run a check now.

Both call `_admin(request)`. Not under a fictional `/api/admin`.

**Panel visibility:** `update.panel` (default `true` when `update.enabled`).
No magic "hide when latest" — visibility is explicit config. Customer installs
keep the panel so ops sees drift, breaking flags, and feed errors during a
change window. The Vue admin nav gains an Update entry only when the API
reports the feature enabled and the principal is admin (nav gating is net-new;
today Admin is always listed and APIs 403).

#### Drift classification (pure function — precedence table)

Inputs: `running_digest`, `desired_digest` (nullable), `channel_digest`
(nullable — from last successful feed), `feed_ok` (bool).

Evaluate top-down; first match wins:

| Order | Condition | Drift |
| --- | --- | --- |
| 1 | `not feed_ok` and no successful check cached | `unknown` |
| 2 | `desired_digest` set and `running_digest` set and they differ | `requested` (v1 has no deadline yet; Appendix A adds `failed` when a deadline expires) |
| 3 | `desired_digest` set and `running_digest == desired_digest` and (`not feed_ok` or `running_digest == channel_digest` or channel unknown) | `current` (intent met; surface feed error beside it if any — do not claim "behind channel" over a met desire) |
| 4 | `feed_ok` and `running_digest` and `channel_digest` and they differ and no desired (or desired == running) | `behind` |
| 5 | `feed_ok` and digests equal (running == channel), and (desired is null or desired == running) | `current` |
| 6 | `running_digest` is null (dev checkout / bake missing) | `unknown` (show commit/version for humans) |
| 7 | else | `unknown` |

**`failed` is deferred to Appendix A.** v1 does not emit it; a rollback that
leaves desired pointing at a newer digest stays `requested` until the operator
clears or updates desired. Clearing desired is an admin action.

**Evaluation order (implementation-authoritative — src/airlie/update/drift.py,
first match wins):**

1. feed down and no channel digest ever cached → `unknown`
2. feed up and `running == channel` → `current` — checked **before** desired, so
   a box running what the channel points at reports `current` even when a stale
   unapplied pin exists
3. desired set and `running == desired` → `requested`
4. feed up and `running != channel` → `behind`
5. feed down and `running` known → `current` — behaviour-level, "still honouring
   what the cache last showed", not "up to date"
6. otherwise → `unknown`

This differs from the design table above (rows 2–5 predate the shipped order;
the operator guide `docs/wiki/ops/update.md` follows the code). For a stale
unapplied desired pin, `manifest_digest = desired or channel` — the response
reports the pin, not the channel target.

`/health` may expose `version` and `image_digest` publicly (ungated, like
today's `commit`). That fingerprints the install — accepted for ops probes;
customers who object can put `/health` behind their ingress ACL. Do not put
desired-release or feed errors on `/health`.

### D. Operator apply path (v1 docs, not code)

v1 ships a corporate runbook; nas.home.apra is the first place we follow it.

1. Panel (or `airlie update check`) shows `behind` and the target digest, plus
   breaking / `schema_epoch` flags for the change ticket.
2. Operator sets `desired` in the panel (optional but recommended — records
   intent on the volume for two-sided drift later).
3. Operator (or Renovate/Flux/human PR) bumps the digest in the declarative
   manifest — git-backed stack, Helm values, or Portainer git stack — pin
   `@sha256:…`, pull policy `IfNotPresent`, pull secret for the mirror.
4. Platform reconciles with **recreate** / `maxSurge: 0` (shared SQLite volume).
5. After boot, panel shows `current` (desired matches running).

Dogfood on nas.home.apra uses step 3 via Portainer; a customer uses the same
steps via their GitOps. airlie's behavior does not branch on which.

## Testing v1

Tiers are named as `docs/wiki/qa/` names them; that catalogue decides what
belongs at each one, so these bullets say what to cover instead of restating its
rules.

- **Unit:** version resolution with metadata present and absent; digest file
  present/absent; the drift classification table above as a pure function;
  `pinned` honesty (a pin is never newer); prerequisite evaluation on plain
  dicts, including a `min` with no `of` and a `package` probe; the update store
  against the test database from `conftest`, not hand-rolled in-memory fakes.
- **Functional:** (the feed is the outside world and gets doubled; the state
  table is our own data layer, so it stays real here): `/v1/admin/update*` on
  `httpx.ASGITransport` with the feed doubled by `respx`, per route covering
  success, 403 for a non-admin, and a validation failure. Plus "current requires
  a successful feed (or met desired)", config load and save keeping `${VAR}`
  verbatim with its original comment, and the degraded response when the feed
  errors.
- **Component:** (`basepack:admin-spa`, jsdom; the surface's first real tests, so
  budget for a `window.matchMedia` stub): badge for each drift value and the
  unconfigured case, `Check now` busy state, the dialog flow, disabled `Apply`
  plus tooltip while stale, the two confirmations, the channel editor (validate
  on blur, preserve an unreadable value), and 403 plus degraded-feed rendering.
- **Contract:** the catalog schema plus the API schema as pure JSON structure,
  using only the `/v1` subset this spec adds.
- **Integration:** one job, no new `deploy/ci/` pipeline: the release workflow
  builds, pushes and signs (cosign) and publishes the catalog, with its push
  permission declared in the job. A container test asserts the image labels, the
  baked digest file, and `/health` from inside the container with **no `.git`
  present** and no network. Plus a real `git archive` run in a temp worktree
  checking the digest file round-trips with the commit metadata, and a manifest
  read against a real tag.
- **No real-clock sleeps anywhere.** If `State._set_initialized` + `sleep(0.2)`
  + fakes pushes a test over 500 ms, `tests/unit/quarantine.toml` applies.

Deferred to B as a **red** suite until the milestone this spec defers lands:
per-endpoint authz (401 / 403 / 422, plus a non-`admin` role that must not reach
`/v1/admin/update*`), the panel's `Apply`, and versioning the update routes
(`/v1` prefix plus `versions.py`). The dashboard shell is covered by its
existing e2e smoke.

## Prerequisites and open questions

1. **Licensing blocks the primary (commercial) customer today.**
   `pyproject.toml` declares PolyForm-Noncommercial-1.0.0 and the repo is
   public. Personal dogfood on nas.home.apra is fine; a company running airlie
   commercially contradicts the declared license. Relicense or grant **before**
   selling this — it is a business gate on the primary use case, not a footnote.
2. **Admin auth before any LAN / cluster expose.** Existing `_admin` /
   principals are enough **if** the install has an admin user or API key and is
   not relying on Mac bootstrap loopback. Bootstrap admin only applies to
   direct loopback with no users. On nas.home.apra and on customer networks:
   complete invite / API key setup before exposing the port; prefer an identity-
   aware front door (Tailscale serve, corporate SSO ingress later).
3. **Registry reach.** ghcr.io is often blocked or rate-limited on corporate
   clusters. v1's answer is channel pointers + `release.json` on a mirror the
   proxy allows, `pinned` for air-gap, drift alarm when the feed fails, and docs
   for registry login / pull secrets. Signature verification (cosign) is noted,
   not designed — expect customers to ask; defer with a clear "not v1."
4. **Packaging must make an installed copy serve its UI** — the web bundle has
   to be in the image, since `web_next` is gitignored.
5. **Air-gapped installs** need an offline bundle (image + wheel + channel
   pointer + `release.json`). `feed: pinned` covers awareness; the bundle
   format is docs-only in v1.
6. **Semver bump mechanism in CI** (auto PATCH on every main vs explicit bump
   commit) — pick during implementation; must yield unique immutable
   `v0.MINOR.PATCH` tags without colliding with the old `v0.1.0+sha` scheme
   (new scheme starts clean; old `+` tags remain wheel-only history).

## Non-goals for v1

Multi-tenancy; applying updates from inside airlie; automated backup/restore;
Helm chart and CRD (documented as later story text); image signature
verification; floating OCI `stable` tag as identity; implementing AIR-55's
self-update loop; any docker/Kubernetes API client in the workload.

---

## Appendix A — deferred: the apply path (still corporate-owned)

v1 stops at awareness + declarative runbook. The deferred half, kept so it is
not re-litigated later — and still without giving airlie a control plane:

**Intent as a durable record.** `desired_release` and `update_window` live in
the database, not on a socket or an annotation. "Apply now" writes
`desired_release = <digest>` plus a one-shot window, and fires the operator's
**configured reconcile hook** (HTTPS URL they own — Portainer git-stack
webhook, Argo CD refresh, Flux receiver). airlie does not hold cluster
credentials; it POSTs to a URL the customer wired to their pipeline. Idempotent
and auditable either way.

**The failure mode declarative reconcile hides:** with a declarative bump,
*nobody reconciling* looks identical to *up to date*. So `desired_release`
carries a deadline; if nothing reports that version by then the panel raises
`failed` ("requested at T, nothing reconciled") instead of staying `requested`
forever. That is when v1's deferred `failed` state appears.

**The alarm that works in both directions:** state lives on the volume, so
whichever version boots reads the same record. A rollback to an older version
therefore *sees* that it is behind intent and reports itself — the failed-
update case a single-sided probe cannot detect. Docs forbid two writers
(`maxSurge: 0`, `Recreate` for compose / Portainer).

**Idle ≠ `line.paused`.** Apply must use a fuller idle gate (line lanes free,
no running serve jobs, bridge idle, no recent `/v1` traffic), then pause the
line, shed new work (503), and only then fire the customer's hook.
`line.paused` alone is necessary but not sufficient. Automatic rollback is an
*intent event* handed to their pipeline — airlie must never restore state and
mutate the cluster from one process. Restore stays an operator action.

**Backup precedent:** there is **no** `scripts/studio-gen/snapshots.py` in tree.
When backup is built, define a new convention under
`state_dir/backups/<release>/` (SQLite online backup API + config + checksum
sidecar). Do not cite a missing script.

**Story text for the airlie board (later):** "Wire up updates beyond awareness:
customer reconcile-hook URL; not-reconciled-by-deadline → `failed`; two-sided
drift; idle-drain before intent; backup+checksum; document `Recreate` /
`maxSurge: 0`; Helm/`AirlieRelease` CRD with digest pin for Kubernetes."
