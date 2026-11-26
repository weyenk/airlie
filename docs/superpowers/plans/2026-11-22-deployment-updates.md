# airlie deployment & update feature — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An operator can tell, from one admin panel and from `/health`, which airlie version this deployment is meant to be running, which it is running, and whether they differ — and an admin can record a pinned *desired* digest. airlie never installs, pulls, restarts, or otherwise mutates its own runtime in v1.

**Architecture:** Prove identity from package metadata + a baked image-digest file; read an immutable `release.json` via an HTTPS channel pointer (or a local `pinned` feed); store last check / last error / desired digest in SQLite beside the board; classify drift with the spec §C digest table; expose `/v1/admin/update*` and a Vue Update tab. Apply is docs-only (operator or GitOps bumps the digest in their manifest).

**Tech Stack:** Python 3.12 / FastAPI / pydantic / stdlib sqlite3 / httpx; Vue 3 + Vitest in `web/`; GitHub Actions release job + `ops/release/Containerfile`.

**Spec:** `docs/superpowers/specs/2026-11-22-update-channel-design.md` (v1 only — Appendix A apply path stays deferred).

## Global constraints

- **Never mutate the running runtime.** No pull, no restart, no pip install, no self-update, no "Update now" that applies. Enforcement: no such call exists + a unit test that asserts the update package never constructs write verbs against the feed or a docker/k8s client.
- **Version is strict semver `MAJOR.MINOR.PATCH`**, never PEP 440-normalised; a parse that would coerce is an error.
- **Honesty:** never claim a version or digest the process cannot prove. Source trees / missing bake file → `version`/`image_digest` unknown/null; a failed feed is never "current" unless a recorded desired digest already matches running (spec §C row 3).
- **`update.enabled: false` is absolute:** no update routes, no ticker, no Update nav entry.
- **Corporate-friendly:** every remote read is HTTPS; allow-listed by config; works behind a proxy; no daemon-set / docker-socket / kubectl from the workload.
- **One place per fact:** package metadata owns version; the feed owns the channel pointer; the store owns last check + desired digest.
- **Single deployment, single target.** There is no py/ts dual half. The hub (`hub/`) is a separate TypeScript service and is out of this plan's surface.
- Tests follow `docs/wiki/qa/` tiers; each surface change updates the matching `docs/wiki/` page in the same PR.

## Review focus

Failure modes the spec implies that bite operators if left untested — each is pinned to a task below:

1. Feed unreachable with a prior good check → panel shows last-known-good + error, never silently `current` (T4/T5).
2. Desired digest met while feed is down → still `current`, with the feed error beside it (T5).
3. Dev checkout (no digest file) → `image_digest: null`, drift `unknown`, never invents a digest (T1/T5).
4. `enabled: false` → `/v1/admin/update` is 404 and the Vue tab is absent (T6/T7).
5. Member principal → GET may 403 (admin-only routes); UI renders the 403 as a sentence, not a stack (T6/T7).

## Adversarial review notes (corrected in this revision)

The previous draft of this plan was internally inconsistent and drifted from the spec. Corrections applied:

| Defect | Was | Now |
| --- | --- | --- |
| Dual py/ts "halves" | Tasks 3–8 treated hub as a second update target | Single airlie target; hub out of scope |
| Routes | Mid-doc invented `/v1/deployment*` | Spec §C: `/v1/admin/update`, `/desired`, `/check` |
| Drift vocabulary | Invented `up_to_date` / `several_behind` / `ahead` / … and a fictional "§E" | Spec §C digest table: `unknown`, `requested`, `current`, `behind` |
| Stored intent | Architecture said "no stored target"; later tasks pinned versions | Spec §C `PUT …/desired` stores a digest; config `feed.kind: pinned` is the air-gap *source*, not the same thing |
| UI copy | "Update now", "applies automatically" | Forbidden in v1 — copy tells the operator the digest to put in their manifest |
| Channel lock | Validator forced `channel == "stable"` | Spec allows `stable` (default) and `latest` (dogfood) |
| Document schema | Mixed tarball-os/arch feed with `release.json` | Spec §B `release.json` + `channels/<name>.json` pointer |
| `load.py` edit | Claimed a manual `update=` kwarg | `AirlieConfig.model_validate(data)` already picks up new sections; only expand `feed.path` if present |
| `/v1/auth/me` | Claimed `email` and invented then retracted `update_panel` | Today: `{user_id, name, role, via}`; add `update_panel: bool` so the nav can gate (spec §C) |
| Broken Task 4 snippets | Syntax-invalid `_ok(...)`, mismatched method names | One store API, used everywhere |
| Shared vocabulary | Contradicted the file map and early tasks | Removed; the Interfaces blocks + file map below are the only names |

## Ground truth (re-verified 2026-11-23; re-check before editing)

- `pyproject.toml` — `version = "0.1.0"`; hatchling build backend.
- `src/airlie/__init__.py` — hardcoded `__version__ = "0.1.0"`.
- `src/airlie/serve/app.py:666` — `FastAPI(..., version="0.1.0")` (OpenAPI metadata); `:857-859` `/health` returns `{status, roles, commit, line}` only; `started_commit = build_commit()` at `:667`.
- `build_commit()` in `src/airlie/story/pipeline.py` — `git rev-parse --short HEAD`, else `None`.
- Admin gate: `src/airlie/auth/api.py:90` `_admin(request)` — `Principal.role == "admin"`. No `require_admin`, no capabilities.
- `GET /v1/auth/me` → `{user_id, name, role, via}` (`auth/api.py:172-175`); Vue `Me` in `web/src/api/auth.ts:9`.
- Config: pydantic on `AirlieConfig` (`schema.py:1118+`); `load_config` is `AirlieConfig.model_validate(data)` (`load.py:276`). `${VAR}` expands only for engine `base_url`/`api_key`.
- Periodic work: `LineTicker` in `src/airlie/serve/ticker.py`, started in `create_app`.
- SQLite pattern: `src/airlie/board/store.py` — `_SCHEMA`, WAL, `check_same_thread=False`, lock on writes.
- Vue: `web/src/components/tabs.ts` (`TABS`); `Tab` union in `web/src/router.ts:7`; screens + colocated Vitest under `web/src/screens/`.
- CI release job: `.github/workflows/pipeline.yml:356` — wheels only today; tag `v${VERSION}+${GITHUB_SHA::7}` (OCI-illegal `+` — image tags must be `v0.MINOR.PATCH` without `+`).
- Test tiers live under `tests/{unit,functional,contract,integration,e2e}`; there is **no** `tests/component/` directory — Vue component tests are Vitest under `web/` (`task web:gate`). Spec "component" bullets map there.
- No `docs/wiki/ops/` yet — create pages this plan names.

## File structure

| File | Holds | Task |
| --- | --- | --- |
| `src/airlie/__init__.py` | `__version__` from `importlib.metadata` | 1 |
| `src/airlie/update/version.py` | strict semver + `is_dev_version` | 1 |
| `src/airlie/update/identity.py` | `RunningRelease`, `running_release`, `SCHEMA_EPOCH`, digest path | 1 |
| `src/airlie/serve/app.py` | `/health` gains `version`/`image_digest`/`schema_epoch`; FastAPI `version=`; router + ticker wire | 1, 6 |
| `src/airlie/config/schema.py`, `load.py` | `UpdateSpec` / `FeedSpec` / `CheckSpec`; expand `feed.path` | 2 |
| `airlie.local.example.yaml` | documented `update:` block | 2 |
| `src/airlie/update/feed.py` | pointer + `release.json` types; HTTPS fetch; pinned directory | 3 |
| `src/airlie/update/store.py` | last check, last error, desired digest, audit | 4 |
| `src/airlie/update/check.py` | one check pass | 4 |
| `src/airlie/update/drift.py` | pure §C classifier | 5 |
| `src/airlie/update/api.py` | `/v1/admin/update*` | 6 |
| `src/airlie/update/ticker.py` | periodic check (`LineTicker` shape) | 6 |
| `src/airlie/auth/api.py` | `/me` gains `update_panel` | 6 |
| `src/airlie/cli.py`, `Taskfile.yml` | `airlie update status\|check` (+ task target) | 6 |
| `web/src/api/update.ts`, `web/src/lib/update.ts`, `web/src/screens/UpdateScreen.vue` | panel | 7 |
| `web/src/api/auth.ts`, `router.ts`, `components/tabs.ts` | `Me.update_panel`, `/update` tab | 7 |
| `ops/release/Containerfile`, `.github/workflows/pipeline.yml`, `scripts/release_feed.py` | image + digest bake + `release.json` + channel pointers | 8 |
| `docs/wiki/ops/update.md`, `docs/wiki/ops/deploying.md` | operator guide + declarative apply runbook | 9 |

---

## Task 1 — Version identity that survives installation (spec §A)

**Files:**
- Modify: `src/airlie/__init__.py`
- Create: `src/airlie/update/__init__.py`, `version.py`, `identity.py`
- Modify: `src/airlie/serve/app.py` (`FastAPI(version=…)` and `health()` body)
- Test: `tests/unit/update/test_version.py`, `tests/unit/update/test_identity.py`

**Interfaces:**
- Consumes: `airlie.story.pipeline.build_commit() -> str | None` (already exists).
- Produces:
  - `airlie.__version__: str` — installed dist version, or `"unknown"`.
  - `parse_semver(raw: str) -> Semver | None`, `compare(a: Semver, b: Semver) -> int`, `is_dev_version(raw: str) -> bool`.
  - `RunningRelease(version: str, git_sha: str | None, image_digest: str | None, schema_epoch: int)`.
  - `running_release(git_sha: str | None, digest_path: Path | None = None) -> RunningRelease`.
  - `IMAGE_DIGEST_PATH = Path("/etc/airlie/image-digest")`, `SCHEMA_EPOCH = 1`, `UNKNOWN = "unknown"`.

- [ ] **Step 1: Write the failing unit tests**

`test_parses_a_plain_release` — `parse_semver("0.4.2")` → `(0,4,2)`, no prerelease/build.

`test_rejects_versions_pep440_would_silently_rewrite` — `"0.4"`, `"0.4.2.1"`, `"1.2.03"`, `"v1.2.3"`, `"1.2.3-"`, `"1.2.3+meta-"` → `None`.

`test_orders_prerelease_before_its_release` — `compare("0.5.0-rc.1", "0.5.0") < 0`.

`test_build_metadata_does_not_change_order` — `compare("0.5.0+a", "0.5.0+b") == 0`.

`test_dev_versions_are_never_selectable_on_stable` — `-rc`/`-dev`/`-alpha`/`-beta` and `"source:dirty"` → `is_dev_version` True; `"0.5.0"` and `"0.5.1+20261122"` → False. Unparsable raw → True (cannot prove it is a release).

`test_missing_digest_file_is_none` — `running_release("abc", digest_path=absent)` → `image_digest is None`.

`test_digest_file_is_read_verbatim` — file contents stripped, no invented `sha256:` prefix.

`test_source_tree_without_metadata_reports_unknown` — patch `importlib.metadata.version` to raise `PackageNotFoundError`; `running_release(...).version == "unknown"`.

- [ ] **Step 2: Run and confirm collection/import failure**

Run: `task test:unit -- tests/unit/update`
Expected: `ModuleNotFoundError: No module named 'airlie.update'`.

- [ ] **Step 3: Implement**

- `__init__.py`: `version("airlie")` with `PackageNotFoundError` → `"unknown"`.
- `version.py`: strict semver regex (no leading zeros); prerelease sorts before release; build ignored in `compare`. Implement real semver prerelease precedence (numeric identifiers vs alphanumeric — do not lexicographically compare `"rc.10"` vs `"rc.2"` as strings).
- `identity.py`: read digest file if present; `schema_epoch=SCHEMA_EPOCH`; version from `airlie.__version__`.
- `app.py`: `FastAPI(version=__version__)`; `health()` adds `version`, `image_digest`, `schema_epoch` from `running_release(started_commit)`. Do **not** put desired-release or feed errors on `/health` (spec §C).

- [ ] **Step 4: Verify**

Run: `uv run python -c "import airlie; print(airlie.__version__)"` → installed dist version (today `0.1.0`).

Run: `grep -rn '__version__ = "' src/airlie/` → no hardcoded assignment left.

Run: `task test:unit -- tests/unit/update` then `uv run ruff check . && uv run ruff format --check . && uv run mypy`.

- [ ] **Step 5: Live smoke**

`task serve` + `curl -s localhost:8090/health` → `"version"` present; `"image_digest": null` on a source tree; `"schema_epoch": 1`.

- [ ] **Step 6: Commit**

```bash
git add src/airlie/__init__.py src/airlie/update src/airlie/serve/app.py tests/unit/update
git commit -m "$(cat <<'EOF'
feat(update): prove the running version from package metadata

/health only reported a commit, so an operator could not tell which release a
deployment was. Version now comes from the installed distribution; a source
tree reports unknown rather than guessing.
EOF
)"
```

---

## Task 2 — The `update:` config section (spec §B)

**Files:**
- Modify: `src/airlie/config/schema.py` (after `ObservabilitySpec`), `src/airlie/config/load.py` (path expand only)
- Modify: `airlie.local.example.yaml`
- Test: `tests/unit/config/test_update_config.py`

**Interfaces:**
- Produces on `AirlieConfig`:
  - `UpdateSpec(enabled: bool = False, panel: bool = True, feed: FeedSpec, check: CheckSpec, registry_token_env: str | None = None)`
  - `FeedSpec(kind: Literal["channel_pointer", "pinned"] = "channel_pointer", base_url: str | None = None, channel: Literal["stable", "latest"] = "stable", path: str | None = None, pinned_version: str | None = None, pinned_digest: str | None = None)`
  - `CheckSpec(interval_s: int = 86_400, timeout_s: int = 10)`

Validation when `enabled` (else skip — defaults must load with no `update:` block):

- `channel_pointer` → non-empty `base_url` starting with `https://`
- `pinned` → `path` **or** (`pinned_version` + `pinned_digest`); if `path`, it must exist at load after expand
- `channel` ∈ `{stable, latest}` only
- no `${VAR}` under `update:` — secrets via `registry_token_env` only

- [ ] **Step 1: Failing tests**

`test_updates_are_off_unless_asked` — bare `AirlieConfig(...)` → `enabled is False`, `panel is True`, `feed.channel == "stable"`, `interval_s == 86400`.

`test_pointer_feed_needs_https_base_url` — `enabled=True, kind=channel_pointer` without `base_url` → `ValueError` matching `base_url`; `http://…` → matches `https`.

`test_pinned_feed_needs_path_or_version_digest` — `enabled=True, kind=pinned` with neither → `ValueError`.

`test_latest_channel_is_allowed` — `enabled=True, channel="latest", base_url="https://example.com/airlie"` constructs.

`test_unknown_channel_is_rejected` — `channel="nightly"` → `ValueError`.

- [ ] **Step 2: Confirm failure** — `AttributeError: 'AirlieConfig' object has no attribute 'update'`.

- [ ] **Step 3: Add specs** on `AirlieConfig` as `update: UpdateSpec = Field(default_factory=UpdateSpec, ...)`.

- [ ] **Step 4: Path expand** — in `load_config`, after other path expands, if `cfg.update.feed.path` is set, resolve it against the config base (same pattern as `story.repo`). Do **not** hand-build `update=` into `AirlieConfig(...)`; `model_validate` already maps the YAML section.

- [ ] **Step 5: Example block** in `airlie.local.example.yaml` (enabled false; comment that dogfood may set `channel: latest`; note `registry_token_env`).

- [ ] **Step 6: Verify** — unit config tests green; `task doctor` unchanged with no `update:` block. Appending `update:\n  enabled: true\n` alone to a local file **must** fail validation naming `update.feed.base_url` (fix Task 2 Step 7 in the prior draft that claimed validation would not fire).

- [ ] **Step 7: Commit**

```bash
git add src/airlie/config/schema.py src/airlie/config/load.py airlie.local.example.yaml tests/unit/config/test_update_config.py
git commit -m "$(cat <<'EOF'
feat(update): config section that refuses a feed it cannot reach

Enabled without an https origin would silently read nothing, so the section is
validated at load: a pointer feed needs base_url, a pinned feed needs a source.
EOF
)"
```

---

## Task 3 — Channel pointer + `release.json` reader (spec §B)

**Files:**
- Create: `src/airlie/update/feed.py`
- Test: `tests/unit/update/test_feed.py`, `tests/contract/update/test_channel_fetch.py`

**Interfaces:**
- Produces:
  - `ChannelPointer(version: str, digest: str, released_at: str, release_json: str)` — the pointer document.
  - `ReleaseJson(version: str, released_at: str, git_sha: str, image: ImageRef, notes: str, breaking: bool, state_migration: bool, schema_epoch: int)` with `ImageRef(repo: str, tag: str, digest: str)`.
  - `FeedError(reason: str, detail: str = "")` with reasons exactly: `"not_configured" | "bad_status" | "unsupported_shape" | "no_digest" | "mismatched_digest" | "dev_on_stable" | "unreachable"`.
  - `FeedResult(changed: bool, release: ReleaseJson | None, pointer: ChannelPointer | None, etag: str | None, checked_at: str)`.
  - `async def fetch_feed(cfg: FeedSpec, *, current_etag: str | None = None, transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 10.0) -> FeedResult`.

Rules:

- `channel_pointer`: `GET {base_url}/channels/{channel}.json` with `If-None-Match` when etag known; then fetch `release_json` URL/path (also HTTPS or relative to `base_url`). 304 → `changed=False`, no body parse.
- `pinned` with `path`: read pointer + release from that directory (no network).
- `pinned` with version+digest only: synthesize a `ReleaseJson` / pointer from config (still no network).
- Refuse empty/non-`sha256:[0-9a-f]{64}` digests (`no_digest` / `mismatched_digest`).
- On `channel=stable`, refuse a release whose version `is_dev_version` (`dev_on_stable`).
- Only `GET`. Never POST/PUT/PATCH/DELETE to the feed.
- `checked_at` is UTC ISO-8601 seconds.

- [ ] **Step 1: Failing unit tests** (httpx `MockTransport`) covering: 304 preserves etag and carries no release; 200 parses pointer then `release.json`; 404/500 → `bad_status`; missing digest → `no_digest`; bad digest form → `mismatched_digest`; empty `base_url` → `not_configured`; `ConnectError` → `unreachable`; `channel=stable` + `0.5.0-rc.1` → `dev_on_stable`; only verb used is GET.

- [ ] **Step 2: Confirm** `ModuleNotFoundError` for `airlie.update.feed`.

- [ ] **Step 3: Implement** `feed.py`. Follow `src/airlie/manager/client.py` / `story/jira.py` for injectable transport + typed errors.

- [ ] **Step 4: Contract test** — assert the fixture JSON in `tests/contract/update/fixtures/` round-trips to `ReleaseJson` and matches the spec §B field set (no extra required keys inventing a second schema).

- [ ] **Step 5: Assert no write verbs** — read `feed.__file__` source; `POST`/`PUT`/`PATCH`/`DELETE` absent.

- [ ] **Step 6: Commit**

```bash
git add src/airlie/update/feed.py tests/unit/update/test_feed.py tests/contract/update
git commit -m "$(cat <<'EOF'
feat(update): read channel pointers and release.json, refuse ambiguity

The reader GETs a pointer then an immutable release document, revalidates with
If-None-Match, and refuses a digest it cannot verify rather than acting on half
of the document.
EOF
)"
```

---

## Task 4 — Store + one check pass (spec §B)

**Files:**
- Create: `src/airlie/update/store.py`, `src/airlie/update/check.py`
- Test: `tests/unit/update/test_store.py`, `tests/functional/update/test_check_pass.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class CheckOutcome:
    checked_at: str          # ISO-8601 UTC
    ok: bool
    reason: str | None       # FeedError.reason when not ok
    release: ReleaseJson | None
    pointer: ChannelPointer | None
    etag: str | None

class UpdateStore:
    def __init__(self, path: Path) -> None: ...
    def record_check(self, outcome: CheckOutcome) -> None: ...
        # always inserts check_result; on ok also upserts pointer_snapshot.
        # a failed pass never clears pointer_snapshot or desired.
    def last_check(self) -> CheckOutcome | None: ...
    def last_good(self) -> CheckOutcome | None: ...   # last ok=True, or None
    def history(self, *, limit: int = 50) -> list[CheckOutcome]: ...  # newest first
    def set_desired(self, digest: str, *, actor: str) -> None: ...
    def clear_desired(self, *, actor: str) -> None: ...
    def desired(self) -> tuple[str | None, str | None]: ...  # (digest, actor)
    def audit(self, *, limit: int = 50) -> list[dict[str, object]]: ...
```

Tables: `check_result`, `pointer_snapshot` (single-row upsert), `desired(digest, actor, at)`, `audit(id, action, actor, at, before_json, after_json)`. Path default: `{state_dir}/update.sqlite` (same WAL/`check_same_thread=False`/lock pattern as `board/store.py`).

`async def run_check(cfg: UpdateSpec, store: UpdateStore, *, transport=None) -> CheckOutcome` — calls `fetch_feed`, records via `record_check`, returns the outcome. Never raises out of a feed failure (failure is a row).

- [ ] **Step 1: Failing store tests**

`test_failed_check_does_not_erase_last_good` — record ok, then unreachable; `last_check().reason == "unreachable"`; `last_good().release.version` still the earlier one.

`test_desired_survives_reopen` — `set_desired` / new store instance / `desired()` matches; `clear_desired` → `(None, None)`.

`test_audit_records_desired_changes` — actor string stored.

- [ ] **Step 2: Failing functional check-pass test** — real store + `MockTransport`; one success then one 503; assert last-good preserved.

- [ ] **Step 3: Implement** store + `run_check`.

- [ ] **Step 4: Verify** unit + functional green; static clean.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/update/store.py src/airlie/update/check.py tests/unit/update/test_store.py tests/functional/update
git commit -m "$(cat <<'EOF'
feat(update): keep last-good check and desired digest across a bad feed

A failed check inserts a row and touches nothing else, so last-known-good is
structural rather than a rule someone has to remember.
EOF
)"
```

---

## Task 5 — Drift classifier (spec §C table)

**Files:**
- Create: `src/airlie/update/drift.py`
- Test: `tests/unit/update/test_drift.py`

**Interfaces:**

```python
Drift = Literal["unknown", "requested", "current", "behind"]

@dataclass(frozen=True)
class DriftResult:
    drift: Drift
    running_digest: str | None
    desired_digest: str | None
    channel_digest: str | None
    feed_ok: bool
    breaking: bool          # from last-good release when present
    state_migration: bool
    schema_epoch_feed: int | None
    manifest_digest: str | None  # the exact string the operator should pin

def classify(
    *,
    running_digest: str | None,
    desired_digest: str | None,
    channel_digest: str | None,
    feed_ok: bool,
    release: ReleaseJson | None = None,
) -> DriftResult: ...
```

Evaluate **top-down, first match wins**. This order is implementation-authoritative — it is what
`src/airlie/update/drift.py` `classify()` actually does, and the parametrize rows mirror it. Row 2
deliberately precedes row 3, so the channel comparison wins before `desired`; row 5 honours the last
confirmed cache rather than claiming freshness. If plan, spec and code ever disagree again, the code
plus `docs/wiki/ops/update.md` win.

| Order | Condition | Drift |
| --- | --- | --- |
| 1 | `not feed_ok` and no cached channel digest | `unknown` |
| 2 | `feed_ok` and running == channel | `current` (before desired) |
| 3 | desired set and running == desired | `requested` |
| 4 | `feed_ok` and running != channel | `behind` |
| 5 | `not feed_ok` and running known | `current` (cache honour; not freshness) |
| 6 | else | `unknown` |

`failed` is **not** emitted in v1 (Appendix A).

Also test: pinned-feed honesty — when `feed_ok` and running == channel from a pinned source, result is `current` and callers label the panel `pinned @ <digest>` (label is UI; classifier still returns `current`).

- [ ] **Step 1: Parametrized table test** — every row above with concrete digests; plus the row-3 feed-down-but-desired-met case.

- [ ] **Step 2: Confirm** missing module.

- [ ] **Step 3: Implement** — compare digests as strings after strip; never semver-compare digests; `manifest_digest` is `desired_digest or channel_digest`.

- [ ] **Step 4: Commit**

```bash
git add src/airlie/update/drift.py tests/unit/update/test_drift.py
git commit -m "$(cat <<'EOF'
feat(update): classify drift from digests using the spec table

Operators and the runbook act on these four states; inventing extra states would
fork the answers the panel and the CLI give.
EOF
)"
```

---

## Task 6 — Routes, ticker, CLI, `/me` gate (spec §B/§C)

**Files:**
- Create: `src/airlie/update/api.py`, `src/airlie/update/ticker.py`, `src/airlie/update/status.py`
- Modify: `src/airlie/serve/app.py`, `src/airlie/auth/api.py`, `src/airlie/cli.py`, `Taskfile.yml`
- Test: `tests/unit/update/test_status.py`, `tests/functional/update/test_update_api.py`, `tests/unit/update/test_cli.py`

**Interfaces:**

- `build_status(cfg, store, running: RunningRelease) -> dict` — keys: `enabled`, `panel`, `feed` (`kind`, `channel`, `base_url`), `running` (`version`, `git_sha`, `image_digest`, `schema_epoch`), `desired_digest`, `channel` (last-good release summary or null), `last_check` (`at`, `ok`, `reason`), `drift` (`DriftResult` fields), `history` omitted on the main GET (separate route).
- Routes (mount **only** when `cfg.update.enabled`; else absent → 404):
  - `GET /v1/admin/update` — `_admin`
  - `PUT /v1/admin/update/desired` — `_admin`; body `{ "digest": "sha256:…" }` must appear on last-good release (or equal running); else 400
  - `DELETE /v1/admin/update/desired` — `_admin`
  - `POST /v1/admin/update/check` — `_admin`; runs `run_check`, returns `build_status`
- `UpdateTicker(checker_coro, interval_s)` — `LineTicker` shape: `start()` runs one check then loops on `wait_for(stop, interval)`; failures become rows, never crash startup; `stop()` cancels.
- `/v1/auth/me` adds `update_panel: bool` = `cfg.update.enabled and cfg.update.panel` (false when section off).
- CLI: `airlie update status` (JSON of `build_status`, no network) and `airlie update check` (runs check; exit 1 if not ok). Taskfile target `update:` beside `doctor:`.

- [ ] **Step 1: Status-shape unit test** — disabled → `enabled: false`, empty available; enabled with patched `running_release` fills running column; `build_status` never opens a network transport.

- [ ] **Step 2: Functional API tests** (ASGI + respx/MockTransport) — admin GET 200; member GET 403; desired PUT happy path + unknown digest 400; check POST records; `enabled: false` → 404 for all four.

- [ ] **Step 3: CLI tests** — `status` with no section → `enabled: false`; `check` without feed → exit 1 / `not_configured`.

- [ ] **Step 4: Implement** api / status / ticker; wire in `create_app` only when enabled (startup starts ticker after `LineTicker.start`, shutdown stops it); extend `me`; add typer sub-app + Taskfile.

- [ ] **Step 5: Live smoke** — `task serve`, unauthenticated `GET /v1/admin/update` → 401/403 not 500; with default config (no update) → 404.

- [ ] **Step 6: Commit**

```bash
git add src/airlie/update src/airlie/serve/app.py src/airlie/auth/api.py src/airlie/cli.py Taskfile.yml tests/unit/update tests/functional/update
git commit -m "$(cat <<'EOF'
feat(update): admin routes, boot check, and CLI over the same status rows

The panel, the ticker, and `airlie update status` all call build_status so an
operator cannot get two answers for the same deployment.
EOF
)"
```

---

## Task 7 — Update tab (spec §C)

**Files:**
- Create: `web/src/api/update.ts`, `web/src/lib/update.ts`, `web/src/screens/UpdateScreen.vue` (+ colocated tests)
- Modify: `web/src/api/auth.ts` (`Me.update_panel`), `web/src/router.ts`, `web/src/components/tabs.ts`

**Interfaces:**
- `getUpdate()`, `setDesired(digest)`, `clearDesired()`, `checkNow()` via `request<T>()`.
- Copy map in `web/src/lib/update.ts` (product surface — exact strings):

| Drift | Sentence |
| --- | --- |
| `current` | `This deployment matches what it should be running.` |
| `current` with `feed_ok: false` | `Can't verify right now — the channel feed is down, so this deployment's digest was not compared with what the channel points to. This is not a claim that it is up to date.` |
| `behind` | `A newer release is on the channel. Pin this digest in your manifest, then let your platform recreate the workload.` |
| `requested` | `Your pin is in effect — this deployment already runs the digest you recorded, so there is nothing to bump. Your manifest should keep pinning: ` + digest |
| `unknown` | `Could not tell — feed error, missing digest bake, or no successful check yet.` |

`requested` means the recorded desired digest equals the digest the host reports running, so the
sentence never tells anyone to bump a pin to what is already running. `current` alone is not a
claim of freshness: with the channel feed down the running digest was never compared with the
channel, which is why that pair gets its own sentence rather than the all-clear one.

Pinned-feed label (when `feed.kind === "pinned"` and drift is `current`): `Pinned @ <digest> — not claiming agreement with upstream stable/latest.`

**Forbidden copy:** "Update now", "applies automatically", "download", any implication that airlie mutates the host.

- [ ] **Step 1: Copy unit tests** — every drift sentence; pinned label; feed-error sentence includes the `reason`.

- [ ] **Step 2: Screen tests** (mirror `AdminScreen.test.ts`) — admin sees desired controls and the manifest digest string; member gets 403 rendered as a sentence and no pin controls; `behind` shows breaking / `state_migration` / `schema_epoch` when the payload carries them; no "Update now" button in any state; `update_panel: false` → tab absent from nav (test `tabs` filter).

- [ ] **Step 3: Implement** screen + api + `Me.update_panel`; add `"update"` to `Tab` and `TABS` gated by `me.update_panel && me.role === "admin"` (or equivalent session filter used elsewhere).

- [ ] **Step 4: `task web:gate` green.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "$(cat <<'EOF'
feat(update): admin Update tab that states drift and the digest to pin

v1 never applies an update from inside the workload; the tab tells the operator
exactly which digest to put in their declarative manifest.
EOF
)"
```

---

## Task 8 — Release image + feed artifacts (spec §A/§B)

**Files:**
- Create: `ops/release/Containerfile`, `scripts/release_feed.py`
- Modify: `.github/workflows/pipeline.yml` (release job)
- Test: `tests/integration/update/test_feed_contract.py` (generated artifacts against fixtures; image label/digest assertions that can run without pushing when `BUILD_RELEASE_IMAGE=1`)

**Behavior:**

1. Assign unique `0.MINOR.PATCH` (implementation picks auto-PATCH-from-run-number vs commit bump — must not reuse the old `v0.1.0+sha` scheme for OCI tags).
2. Build wheel **and** `ops/release` image in one job; fail the release if either fails.
3. Push `<registry>/airlie:v0.MINOR.PATCH` (immutable; **no `+` in the tag**); record digest.
4. Bake `/etc/airlie/image-digest` (rebuild-with-ARG or equivalent — one advertised digest).
5. Publish GitHub Release with wheel + `release.json` (spec §B shape).
6. Update `channels/latest.json` every release; `channels/stable.json` only on an explicit promotion step/input. Promotion of a release with `state_migration: true` without notes fails the promotion step. (As shipped: the guard exists and reads the published document, but the release job passes neither flag, so no CI-published release can reach it — see the note under "Release flags" in `docs/wiki/ops/update.md`.)
7. Image: non-root uid 1000, web bundle built in (spec prerequisite — `web_next` is gitignored), `CMD ["airlie", "serve"]`, OCI labels `{version, revision, created}`. (As shipped: the `CMD` also passes `--host 0.0.0.0`, which is what makes the published-port path in `docs/wiki/ops/deploying.md` work; the guard there is `auth.trusted_proxies`.)

- [ ] **Step 1: Containerfile** modelled on `ops/test/Containerfile` but runtime-only deps + web build + digest file path.

- [ ] **Step 2: `scripts/release_feed.py`** — render `release.json` + channel pointer JSON from CLI args; unit-test the renderer with fixed inputs → exact JSON.

- [ ] **Step 3: Wire the release job**; keep wheel-only rollback path documented if registry creds absent (job should fail closed on missing registry when `release_image: true`, not publish a half release).

- [ ] **Step 4: Integration** — renderer output validates against `ReleaseJson` / `ChannelPointer`; optional container smoke: `/health` inside image with no `.git` reports baked digest and metadata version.

- [ ] **Step 5: Commit**

```bash
git add ops/release scripts/release_feed.py .github/workflows/pipeline.yml tests/integration/update
git commit -m "$(cat <<'EOF'
feat(update): publish image digests and immutable release.json

A GitHub Release of wheels alone could not tell a deployment which digest to
run; the release job now publishes the image, the baked digest, and channel
pointers as separate reviewed artifacts.
EOF
)"
```

---

## Task 9 — Operator docs (spec §D)

**Files:**
- Create: `docs/wiki/ops/update.md`
- Create or edit: `docs/wiki/ops/deploying.md` (declarative apply runbook; `Recreate` / `maxSurge: 0`; pull policy `IfNotPresent`; pull secrets)

**Content (exact runbook steps from spec §D):**

1. Panel / `airlie update check` shows `behind` + target digest + breaking / schema_epoch.
2. Optional: set desired in the panel (records intent).
3. Bump digest in declarative manifest (`@sha256:…`).
4. Platform reconciles with recreate / `maxSurge: 0`.
5. Panel shows `current`.

State clearly: airlie does not call Portainer/Kubernetes/docker. Licensing gate (PolyForm-Noncommercial) called out as a business prerequisite for commercial customers (spec prerequisites §1) — docs, not code.

- [ ] **Step 1: Write the pages.**

- [ ] **Step 2: Commit**

```bash
git add docs/wiki
git commit -m "$(cat <<'EOF'
docs(update): operator guide and declarative apply runbook

v1 stops at awareness; the runbook is the apply path until Appendix A lands.
EOF
)"
```

---

## Self-review (against the spec)

| Spec section | Task |
| --- | --- |
| §A identity, baked digest, `/health` fields, release image | T1, T8 |
| §B `release.json`, channel pointers, adapters, config, ticker/CLI check | T2, T3, T4, T6 |
| §C panel routes, desired, drift table, `/health` boundary, nav gate | T5, T6, T7 |
| §D operator apply runbook | T9 |
| Testing bullets (unit drift/store, functional routes, contract schema, integration image) | per-task tests |
| Non-goals (no in-app apply, no docker/k8s client, no AIR-55 loop) | Global constraints + T7 forbidden copy |
| Appendix A | Explicitly out of scope |

Gaps intentionally deferred with the spec: cosign, Helm/CRD, `failed` drift, reconcile-hook URL, idle-drain, backup.
