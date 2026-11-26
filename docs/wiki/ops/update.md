# Update awareness and drift (v1 operator guide)

This page describes what the admin panel's Updates tab, `airlie update status`, and
`airlie update check` report,
the four drift states, configuration keys, and what the system **does not** do.

## What the panel and CLI report

The admin panel (visible to admins when `update.enabled: true` and
`update.panel: true`) and `airlie update status` (read-only, no network) both
display information drawn from the local SQLite ledger — never the outside
world. The panel also exposes `airlie update check` (CLI: `airlie update check`)
which runs one feed fetch and writes the result to that ledger.

Both surfaces report:

- The **running release**: version, git sha (when a dev checkout), image
  digest (the baked-in `sha256:<hex>` string, exactly as the release job
  wrote it — the `@` you add in a manifest is not part of it — or `null` on a
  dev checkout), and the schema epoch compiled into this build. (`src/airlie/update/status.py` ~53–60;
  `serve/app.py` ~934–937)
- The **channel state**: the last release the channel pointer feed answered with,
  including its version, git sha, digest, `breaking` / `state_migration` flags
  (both false on any release the release job published — see "Release flags"
  below). (`src/airlie/update/status.py` ~30–45)
- The **desired digest**: the operator's pin, recorded in the ledger, or `null`
  if unset. (`src/airlie/update/api.py` ~58–67)
- The **drift classification** (see below). (`src/airlie/update/drift.py` ~45–80)
- **`feed_unavailable`**: a flag that is `true` when the last feed check failed.
  (`src/airlie/update/status.py` ~71)

## The four drift states

Drift is a **pure function** of four inputs: the running digest, the desired
digest (nullable), the channel pointer digest (nullable — from the last
successful feed fetch), and whether that last check succeeded (`feed_ok`).
It evaluates these conditions in order, first match wins:

| Condition (evaluate top-down) | Drift state |
| --- | --- |
| Feed never succeeded (no cached check). | `unknown` — nothing authoritative to compare. Pin a digest yourself: either set it in the panel or put `@sha256:<hex>` directly in your manifest. |
| Feed succeeded, running digest and channel digest both known, and they match. | `current` — you are running what the feed says is available right now. |
| Desired digest was set by an admin and matches the running digest (regardless of feed). | `requested` — intent is met; no change to apply. (Note: v1 has no deadline yet for stale desired pins.) |
| Feed succeeded, running digest and channel digest both known, they differ, no desired (or desired differs from running). | `behind` — the channel pointer names a newer digest. The drift response carries `manifest_digest`: when no desired is set, this is the channel target; **when a stale (unapplied) desired pin exists** — one that does not match the running digest — `manifest_digest` reports the stale pin, **not** the channel target. |
| Feed failed and running digest is known. | `current` — honour what the cache last showed (does not imply "up to date" with respect to the live channel). |
| Running digest is `null` (dev checkout, baked digest missing). | `unknown`. |

### Release flags: carried end to end, never set by CI

`breaking` and `state_migration` travel the whole path — `release.json` → the
reader → `DriftResult` → the panel's badges — and `promote-stable` refuses to
point stable at a release with `state_migration: true` and empty notes. The
feed's `schema_epoch` is shown in the drift result as `schema_epoch_feed`,
next to the epoch compiled into the running build; nothing compares them for
you.

What the release job does **not** do is set either flag: `render-release` runs
without `--breaking` / `--state-migration`, so every CI-published release reads
as non-breaking and that promotion guard is unreachable from the pipeline. The
reason is in the comment above the step in
[`.github/workflows/pipeline.yml`](../../../.github/workflows/pipeline.yml) — a
push has no honest source for "was this a breaking MINOR bump". Until that
changes, read a false badge as *not published as breaking*, and judge
compatibility from the release notes and the diff.

Source: [`src/airlie/update/drift.py`](../../src/airlie/update/drift.py) (pure function, no I/O).

### Why a dev checkout reports `unknown` instead of guessing

A local development checkout does not bake the image digest into the running
process (there is no `/etc/airlie/image-digest` file, and `git rev-parse --short
HEAD` may not be available). The code detects this case — `read_image_digest()`
returns `None` (`src/airlie/update/identity.py`) — and reports no
digest rather than a guess. Without an image digest, the drift classification
falls to `unknown` (rows 1 and 6). This is intentional: a dev checkout cannot
verify whether it is up to date with respect to the published feed.

The same `None` covers a bundle file that exists but cannot be believed: only
the `sha256:<64 lowercase hex>` form the release job bakes is accepted, so a
file left by a hand-run `docker build`, a hand edit, or a truncated value reads
as no digest. Passing such a value through would have been worse than
reporting nothing — drift compares the string byte for byte against the
registry's, so it could never match, and the panel would both claim `behind`
forever and offer that unrunnable string as the digest to pin.

The CLI `airlie update status` still shows `version` and `git_sha` (from
`build_commit()`) so a developer can identify the build by hand. The panel
shows commit/version text beside "unknown" for human triage, but does not try to
classify it.

## What `desired` records and what it does **not** do

The panel exposes a "Set desired digest" dialog (admin only) and the API route
`PUT /v1/admin/update/desired`. This **records intent** in the SQLite ledger —
it does not restart or redeploy anything.

The CLI also exposes:

- `airlie update status` — read-only snapshot of the current state (feed, running
  release, desired pin, drift). No network. (`src/airlie/cli.py` ~476–498)
- `airlie update check` — runs one feed fetch now, writes the result to the ledger,
  exits non-zero when the check failed. (`src/airlie/cli.py` ~500–523)

The panel's "Check now" button calls `POST /v1/admin/update/check`, which runs
the ticker's same feed check on demand.

### Validation: only published digests are accepted

A desired digest must match either the last-good release's digest or what is
currently running (`src/airlie/update/api.py` ~53–60). An unknown / unpublishable
digest is refused with a 400. This prevents pinning an invalid digest that would
strand the operation: deploy, then roll back to a version nobody published.

## Configuration surface

Everything is under the `update:` top-level section in `airlie.yaml`. The product
defaults are off (`enabled: false`) — the section must be explicitly present.

```yaml
update:
  enabled: true           # required; off = no routes, no ticker, no nav entry
  panel: true             # hidden from the admin nav when false (default true)
  feed:
    kind: channel_pointer # or "pinned" — required field, defaults to "channel_pointer"
    base_url:             # required for channel_pointer; must be https://
      "https://your.mirror.example.com/airlie-releases"
    channel: stable       # "stable" (default) or "latest"; dogfood uses latest
    # For kind: pinned (air-gap / change window) there is no base_url to follow. `path` is a
    # directory holding the same documents the channel serves — <path>/channels/<channel>.json
    # plus the release document that pointer names, resolved under <path>:
    #   kind: pinned
    #   path: /local/releases
    # Or name the pin with no directory at all, which needs both fields:
    #   kind: pinned
    #   pinned_version: "2026.11.3"
    #   pinned_digest: "sha256:<hex>"
  check:
    interval_s: 86400     # ticker interval in seconds (minimum 3600)
```

Config keys are validated at load time (`src/airlie/config/schema.py` ~644–680).
The `_validate_update` method rejects:

- `enabled: true` with no valid feed (channel_pointer must have
  `https:// base_url`; pinned must have a path or version+digest pair).
- Literal `${VAR}` references anywhere under `update:` — the config loader's
  template expansion only covers engine blocks, so `airlie` refuses to interpret
  them under update (prevents confusing network errors at runtime).

### How `update.enabled: false` works

When the section is absent or `enabled: false`:

- **No API routes exist.** The update router (`/v1/admin/update*`) is never
  mounted into the FastAPI app. (`src/airlie/serve/app.py` ~730–735: the router
  is built only when `update_store` is created, which itself checks `cfg.update.enabled`).
- **No ticker runs.** The periodic feed checker is not started. (`src/airlie/serve/app.py` ~733)
- **No nav entry.** The session object returned by `GET /v1/me` includes
  `update_panel: false` (the auth API routes in `src/airlie/auth/api.py` ~185),
  and the panel hides the Update tab from navigation.

The panel still reports `enabled: false` in its response body — a present,
quiet answer rather than an obscure 404.

## The licensing gate

The primary customer for this feature (a corporation running airlie behind a
corporate proxy with GitOps) is **blocked by the project's current license**.

The repository declares `PolyForm Noncommercial License 1.0.0` in its root
`LICENSE` file. A commercial customer distributing or operating airlie on their
network contradicts this license's terms. This is a **business prerequisite for
docs, not code**: the documentation pages above describe how to operate within
those constraints (digest pinning, air-gap mirrors, offline bundles), but a
company cannot use them commercially until the license is changed or an
alternative is granted.

Releases `PyPI` under a permissive license for commercial use, or grant
a separate commercial license. This gate is called out on this page so it
is not revisited during a security review as an afterthought.

## What airlie does **not** do

- It does not call Portainer, Kubernetes, or any `docker` socket.
- It does not restart containers or redeploy services.
- It does not apply an update from inside the application process.
- It has no `kubectl`, Helm, or CRD integration.

Nothing in the `/v1` update routes performs a deployment action. All that
exists is awareness (the feed checker, the panel) and intent recording
(the desired pin). The actual application of a newer version is done through
the operator's own tooling — Portainer git-backed stacks, Flux image policy,
or a human bump. See [`docs/wiki/ops/deploying.md`](deploying.md) for the
declarative apply runbook.
