# Declarative apply runbook

This is the operational procedure for moving a running airlie deployment to a
newer version. The steps follow the order defined in spec
[`2026-11-22-update-channel-design.md` §D].

## v1 scope: awareness + declarative runbook, not a button

v1 stops at **awareness and intent**. The application does not apply an update.
Nothing in `/v1` calls Portainer, Kubernetes, or a docker socket (`src/airlie/update/api.py`
header comment; `serve/app.py` ~730–735). The operator or their GitOps tooling
changes the digest in a declarative manifest, and their platform applies it.

## The two digests, and which one goes in your manifest

The release image is built in two pushes because an image cannot contain its
own manifest digest (`.github/workflows/pipeline.yml` release job,
`ops/release/Containerfile`). Both digests are published in `release.json`, and
they answer different questions:

| Value | What it is | Where it belongs |
| --- | --- | --- |
| `image.pull_digest` | the registry digest of `<repo>:<tag>` — the image you actually pull | **your manifest's `image: …@sha256:<pull_digest>`** |
| `image.digest` | the digest baked into `/etc/airlie/image-digest`, which the running container self-reports | compared by the drift table; never pinned in a manifest |

They are different values for one release, and neither can be computed from the
other: the container can state its own `image.digest` but never its registry
digest, and the registry knows the pull digest but nothing the container says.

**Pin the pull digest.** Pinning `image.digest` deploys the *pre-bake* build,
whose digest file is empty, so the container reports nothing about itself and
the panel can never compare it with the channel — the drift state sticks at
`unknown` for as long as that pod runs.

### Releases without a pull digest

A `release.json` published before `pull_digest` existed carries only
`image.digest`. `manifest_digest` then falls back to that value and the panel
recommends it, because the feed has nothing better to offer; following it lands
in the `unknown` state above. Re-render the release from the pushed image (see
"Air-gap and pinned mirrors") once its registry digest is readable, or pin by
reading the tag yourself: `docker buildx imagetools inspect <repo>:<tag>`
reports exactly the `pull_digest` value.

## The five steps

### Step 1 — Panel / CLI shows `behind` + target digest + breaking

Run the check (from the panel's "Check now" button, or `airlie update check`)
until the drift state is `behind`. The response carries:

- **Target digest** (`manifest_digest` on the drift object) — exactly what to
  pin. With no `desired` recorded it is the release document's
  `image.pull_digest`, the registry's digest for the image tag; a recorded
  `desired` is reported in its place (`src/airlie/update/drift.py` `classify`).
  See "The two digests" above: `image.digest`, in the same document, is a
  different value and not the one to pin.
- **Breaking / `schema_epoch`:** the target release's `breaking` and
  `state_migration` badges, and the feed's `schema_epoch`
  (`schema_epoch_feed` on the drift object) beside the epoch compiled into what
  you are running. Nothing compares the two epochs for you, and a false
  `breaking` badge means "not published as breaking", not "verified
  compatible" — the release job writes both flags false (see
  [update.md](update.md), "Release flags"). Read the release notes and the diff
  yourself, and note what you find in the change ticket.

### Step 2 — Optionally set `desired` (records intent)

In the admin panel, click "Set desired digest" and enter the target
`sha256:<hex>`. This writes a row to the local SQLite ledger — it does not
redeploy anything. Record the pull digest you are about to deploy, so the ledger
names the same build your manifest will. Before deploy, while the new image is
yet to be pulled, the panel reports `behind` (running != desired), and after
your platform reconciles it reports `current`.

What `desired` does *not* do is hold the `requested` state for a pull-digest
pin. Row 3 of the drift classifier fires on `desired == running`, and the
running digest is the container's self-report (`image.digest`) — a value that is
never the registry digest you pinned (that is the whole reason the build is two
pushes). So a pull-digest pin reads `current` while the channel still points at
that release, and `behind` once the channel moves ahead of it. If you want the
hold that says "I have decided to stay on this build, stop calling it behind",
record the release's `image.digest` instead — the one value this deployment can
confirm it is running. Both are accepted pins (step 2's PUT validates against
the last-good release's two digests plus what is running).

**This is a v1 decision, not a defect to route around.** A pull-digest
`desired` is an audit breadcrumb: it records which build you meant to deploy,
and nothing more. Do not expect `requested` from it. Row 3 is deliberately left
as `desired == running` rather than widened to a condition it could never
reach — the honest pull-pinning equivalent needs the pin to carry both the pull
digest and that release's baked self-report, and a container cannot read its
own registry digest (`src/airlie/update/drift.py` `classify`). Nothing is
scheduled to change that; the deferred apply path in spec Appendix A is a
separate piece of work and does not close this gap. Self-report pins still get
`requested`, as above.

The CLI equivalent:

```bash
curl -X PUT https://your.airlie.example.com/v1/admin/update/desired \
  -H "Authorization: Bearer <admin-key>" \
  -d '{"digest": "sha256:abcdef..."}'
```

The API validates the digest against what the last-good release publishes (both
of its digests) and what is running, and rejects anything else with HTTP 400
(`src/airlie/update/api.py` `_known_digests`).

This step is optional but recommended — it decouples awareness ("we should
update") from action (the manifest bump). If you skip it, the panel reports
`behind` until your manifest is updated and the next ticker cycle confirms.

### Step 3 — Bump the digest in your declarative manifest

Change `image: <registry>/airlie@sha256:<target>` in your declarative manifest,
where `<target>` is the **pull digest** (`release.json`'s `image.pull_digest`,
the `manifest_digest` the panel showed you in step 1). Not `image.digest`: that
one pulls the pre-bake build (see "The two digests").

Example for a Docker Compose / Portainer git-backed stack:

```yaml
services:
  airlie:
    image: ghcr.io/<org>/airlie@sha256:<pull-digest>  # bump from <old> to <new>
    imagePullPolicy: IfNotPresent                     # required; see below
```

Or for Kubernetes (the Helm values or CRD your platform manages):

```yaml
spec:
  template:
    spec:
      containers:
        - name: airlie
          image: ghcr.io/<org>/airlie@sha256:<pull-digest>  # bumped
          imagePullPolicy: IfNotPresent                     # required
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
drift = `current`.

Read `current` for what it actually checked: the container's self-reported
digest against the digest the channel advertises as its self-report
(`image.digest`). It never compares your manifest's pull pin — no process inside
the image can know that value. Getting `current` here is the confirmation that
you pinned the right build: had you pinned `image.digest` instead, you would be
running the pre-bake image and this line would read `unknown`.

A `desired` pin from step 2 does not change this answer. It changes what you see
later: a self-report pin holds `requested` when the channel moves ahead or the
feed goes down (spec §C row 3), while a pull-digest pin holds nothing, so the
row below it reports `behind` as soon as the channel points somewhere newer.

## Air-gap and pinned mirrors

For air-gapped deployments (no egress to the public registry), use
`feed: kind: pinned` with `update.feed.path` — a **directory**, not a manifest
file — or explicit version + digest. The panel reports `pinned @ <digest>`
when the feed answers but does not imply agreement with upstream.

To fill a pinned mirror after a new release:

1. Run `scripts/release_feed.py render-release` and
   `render-pointer` (invoked by the CI release job) against your local
   build. (`scripts/release_feed.py` — pure renderer, no network) Pass
   `--image-digest` the digest you baked into the image and
   `--image-pull-digest` the registry digest of the tag you mirrored —
   `docker buildx imagetools inspect <repo>:<tag>` reads it back. Omit the
   latter only if the mirror has no registry push to name, and the operators
   reading it then have nothing to pin.
2. Place the resulting files under that directory with the names the feed
   reader looks for: the pointer at
   `<update.feed.path>/channels/<channel>.json` (`channel` is
   `update.feed.channel`, `stable` by default), and the release document
   where that pointer's `release_json` says, resolved under
   `<update.feed.path>` — a document outside the directory is refused.
   (`src/airlie/update/feed.py` `_read_pinned_documents`.)

The release image is built by `.github/workflows/pipeline.yml` (release job)
and baked with `ARG IMAGE_DIGEST` via a two-push build into
`/etc/airlie/image-digest`: the first push's digest is what the image
self-reports (`release.json`'s `image.digest`), and the second push is the image
you deploy, whose registry digest the same document publishes as
`image.pull_digest`. (`ops/release/Containerfile`)

## Reaching the image over a published port

The image starts `airlie serve --host 0.0.0.0` on port 8090. The CLI's own
default stays `127.0.0.1`: a container has its own network namespace, so
loopback inside it is reachable by nothing outside it, and an image that bound
the default refused every connection while its logs looked normal.

Binding a routable address changes who can **connect**. Trust still comes from
the TCP peer (`src/airlie/auth/principal.py`): remote peers arrive with their
own address and must carry an API key or a session cookie. The fresh-install
**bootstrap admin is a loopback-listen convenience only** — `airlie serve`
default `--host 127.0.0.1` on the Mac. With `--host 0.0.0.0` (this image):

1. serve **refuses to start** unless an API key will be accepted (`--api-key`,
   `AIRLIE_SERVE_API_KEY`, or the key `ensure_serve_api_key_stores` mints into
   `~/.config/airlie/bridge.env` on first boot); and
2. bootstrap is **off** (`allow_bootstrap=False`), so nothing that can still
   talk to loopback inside the container (docker exec, an undeclared local
   proxy) inherits admin.

If anything sits in front of airlie — nginx, Traefik or HAProxy in the same
container or pod, a mesh sidecar, or `tailscale serve` running in its own
container — list that proxy's address or CIDR in `auth.trusted_proxies`
(for example `[127.0.0.1]` or `[172.17.0.2]`). Only from a listed peer are
`X-Forwarded-For`, `X-Forwarded-Proto` and `Tailscale-User-Login` read. Left at
its empty default no proxy is trusted, so one you did not declare can assert
neither a client identity nor a protocol.

Two further consequences to accept before publishing the port:

1. Declaring a **loopback** address in `trusted_proxies` also means loopback
   stops counting as the machine itself on a loopback-only listen — create an
   admin, or set an API key, first.
2. Reaching airlie through `tailscale serve` on loopback needs
   `auth.tailscale: true`, which is what makes the headers it forwards count.
   Without it `X-Forwarded-Proto` is ignored and the session cookie is not
   marked `Secure`.

Finish invite and API-key setup before you expose the port. On this image the
first admin uses the minted serve key (read it from the container's
`~/.config/airlie/bridge.env`, or set `AIRLIE_SERVE_API_KEY` yourself) as
`Authorization: Bearer …`, then invites people from the Admin tab — not the
bootstrap principal.

## Licensing gate reminder

Commercial customers operating airlie on their network are blocked by the
project's `PolyForm Noncommercial License 1.0.0`. This is a business gate —
relicense or obtain commercial licensing before deploying this feature in a
commercial environment. See [`docs/wiki/ops/update.md`](update.md) for details.
