# Declarative apply runbook

This is the operational procedure for moving a running airlie deployment to a
newer version. The steps follow the order defined in spec
[`2026-11-22-update-channel-design.md` §D].

## v1 scope: awareness + declarative runbook, not a button

v1 stops at **awareness and intent**. The application does not apply an update.
Nothing in `/v1` calls Portainer, Kubernetes, or a docker socket (`src/airlie/update/api.py`
header comment; `serve/app.py` ~730–735). The operator or their GitOps tooling
changes the digest in a declarative manifest, and their platform applies it.

## The five steps

### Step 1 — Panel / CLI shows `behind` + target digest + breaking

Run the check (from the panel's "Check now" button, or `airlie update check`)
until the drift state is `behind`. The response carries:

- **Target digest** (`manifest_digest` on the drift object) — exactly what to
  pin. It is a `sha256:<64-hex>` string from the channel pointer's release
  document (`release.json`'s `image.digest`). (`src/airlie/update/drift.py` ~75–80)
- **Breaking / `schema_epoch`:** whether the target release has
  `breaking: true` or a higher `schema_epoch` than what you are running. These
  must be noted in your change ticket.

### Step 2 — Optionally set `desired` (records intent)

In the admin panel, click "Set desired digest" and enter the target
`sha256:<hex>`. This writes a row to the local SQLite ledger — it does not
redeploy anything. The benefit: after you have deployed the pinned digest, a
channel that moves ahead reports `requested` (you are running what you asked for)
instead of `behind`. Before deploy, while the new image is yet to be pulled,
the panel reports `behind` (running != desired) — the pin prevents that from
ever degrading to a stale `behind` once deployed, because row 3 of the drift
classifier (`desired is not None and running == desired` → `requested`) always
wins over row 4 (`behind`).

The CLI equivalent:

```bash
curl -X PUT https://your.airlie.example.com/v1/admin/update/desired \
  -H "Authorization: Bearer <admin-key>" \
  -d '{"digest": "sha256:abcdef..."}'
```

The API validates that the digest was published (on last-good or running) and
rejects unknown values with HTTP 400 (`src/airlie/update/api.py` ~53–60).

This step is optional but recommended — it decouples awareness ("we should
update") from action (the manifest bump). If you skip it, the panel reports
`behind` until your manifest is updated and the next ticker cycle confirms.

### Step 3 — Bump the digest in your declarative manifest

Change `image: <registry>/airlie@sha256:<target>` in your declarative manifest.

Example for a Docker Compose / Portainer git-backed stack:

```yaml
services:
  airlie:
    image: ghcr.io/<org>/airlie@sha256:<target>  # bump from <old-sha> to <target>
    imagePullPolicy: IfNotPresent                  # required; see below
```

Or for Kubernetes (the Helm values or CRD your platform manages):

```yaml
spec:
  template:
    spec:
      containers:
        - name: airlie
          image: ghcr.io/<org>/airlie@sha256:<target>  # bumped
          imagePullPolicy: IfNotPresent                 # required
```

**`imagePullPolicy: IfNotPresent` is important.** Never use `Always`: on a
rate-limited registry (common in corporate environments), the constant retry
loop on `ImagePullBackOff` can exhaust your API quota. `IfNotPresent` pulls
only when the image is not already cached locally, which is correct for a
digest-pinned deployment (a digest never changes once published).

If your registry requires authentication, include a **pull secret** (`imagePullSecrets`)
in addition to the digest pin.

### Step 4 — Platform reconciles (recreate / `maxSurge: 0`)

Your platform applies the updated manifest. Two constraints apply:

1. **`Recreate` strategy** (or `maxSurge: 0`). The SQLite stores use Write-Ahead
   Logging and a single database file. Two running versions writing the same
   volume simultaneously corrupts the store (`docs/superpowers/specs/...`
   §Corporate-friendly rules #7). `Recreate` ensures the old pod is killed
   before the new one starts, so only one version holds the volume at a time.
2. **`maxSurge: 0`** for any rolling-update-capable platform (Kubernetes
   Deployments, similar constructs in other orchestrators). This enforces
   the same single-writer invariant without requiring `Recreate`.

Your platform's own mechanism (Flux image policy, Portainer git-sync, a human
pull request) handles this step. airlie does not participate.

### Step 5 — Panel shows `current`

After the new version boots, the next ticker cycle (default 86400 seconds)
or a manual `airlie update check` re-reads the channel, and the panel reports
drift = `current`. The desired digest matches what is running — intent is met.

If you set a desired pin in step 2, the panel transitions directly to
`current` (desired == running, feed confirms). If you skipped step 2, it goes
through `current` once the feed answers match your now-running digest.

## Air-gap and pinned mirrors

For air-gapped deployments (no egress to the public registry), use
`feed: kind: pinned` with a local path (`update.feed.path`) or explicit
version + digest. The panel reports `pinned @ <digest>` when the feed
answers but does not imply agreement with upstream.

To fill a pinned mirror after a new release:

1. Run `scripts/release_feed.py render-release` and
   `render-pointer` (invoked by the CI release job) against your local
   build. (`scripts/release_feed.py` — pure renderer, no network)
2. Place the resulting `release.json` and channel pointer under your pinned
   mirror's path (or run a local copy of the script against your build).

The release image is built by `.github/workflows/pipeline.yml` (release job)
and baked with `ARG IMAGE_DIGEST` via a two-push build into
`/etc/airlie/image-digest`. (`ops/release/Containerfile`)

## Licensing gate reminder

Commercial customers operating airlie on their network are blocked by the
project's `PolyForm Noncommercial License 1.0.0`. This is a business gate —
relicense or obtain commercial licensing before deploying this feature in a
commercial environment. See [`docs/wiki/ops/update.md`](update.md) for details.
