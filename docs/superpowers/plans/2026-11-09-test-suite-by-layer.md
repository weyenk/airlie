# Test suite by layer, PR 1: layers that run, main green — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every whole test file that is not a unit test leaves `tests/unit` for its layer, every layer runs in one networkless Linux image locally and in CI, and main's unit job is green with no new quarantine lines.

**Architecture:** `scripts/run_layer.py` already runs `tests/<layer>/`; this PR gives it a Linux image to run in (`ops/test/Containerfile`, Apple `container` locally, Docker in CI, outside network off), moves shared helpers into an importable `tests/support/` package, and moves whole files by the approved triage. Obsolete tests are pruned first so nothing dead is carried across.

**Tech Stack:** Python 3.12, uv, pytest + xdist, Taskfile, Apple `container` 1.5 / Docker, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-09-test-suite-by-layer-design.md` (the approved design; Decisions 1, 3, 6, 7 and the Relocate/rewrite/delete rules govern this PR). PRs 2–4 get their own plans after this one lands.

## Global Constraints

- Layer definitions are `skills/qa/{unit,functional,contract,integration,e2e}.md`; a test's layer follows what it does, not its file name.
- Triage of record: `/Users/noahkruswicki/.claude/jobs/c702acf1/tmp/triage-a.csv` and `triage-b.csv` (columns `file,verdict,tests,reason,fix,quarantined_count`). Whole-file moves in this PR: the 24 integration, 11 functional and 4 contract files listed in `/Users/noahkruswicki/.claude/jobs/c702acf1/tmp/triage-merged.txt` (after Task 1's prune, re-checked).
- Moved tests keep their bodies (PR 3 rewrites); only imports change to `tests/support/`.
- Every moved node id leaves `tests/unit/quarantine.toml` in the same commit (the stale-entry check fails otherwise). No new quarantine lines.
- Keep `_no_mutation_harvest` (autouse, `tests/unit/conftest.py`) for unit; any layer that runs Line/factory beats gets the same guard. `tests/unit/conftest.py` stays at its path (`scripts/mutate_changed.py` SETUP_FILES names it).
- The image: outside network off, loopback on; dependencies baked in at build; the checkout mounted read-only, the venv outside it (as `ops/e2e/Containerfile` does). Torch/CUDA skipped as in the e2e image; tests needing mlx or macOS `sandbox-exec` carry `@pytest.mark.mac` and are skipped in the image.
- Local AGENTS.md gate: unit on the host; functional, contract, integration through the image. CI: all four in the image.
- Per-test timeouts: integration 60 s, functional and contract 30 s (pytest-timeout if it is already a dependency; otherwise a conftest alarm — do not add a dependency without noting it in the report).
- Gate for every task (repo root): `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`, plus `task test:functional`, `task test:contract`, `task test:integration` once they exist.
- Commit subjects start `tests:`; end every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never `git clean`; never push; never touch `scripts/mutate_*.py`, `.github/workflows/mutation-*.yml` or the mutation jobs in `pipeline.yml` (another session owns them).

## Review Focus

1. A moved test that silently stops being collected (wrong directory, missing `__init__.py`/conftest, an import that now fails at collection and is swallowed) — every layer's collected count must equal what moved in.
2. A test that passed on the Mac host because of host state (git identity, `~/.gitconfig` insteadOf, a cached model, a real network) failing in the image — the image must make that visible, not paper over it.
3. Fixtures defined in `tests/unit/conftest.py` that moved tests relied on (autouse env scrubbing, `_no_mutation_harvest`, the ATLASSIAN env scrub, HAS_MLX) — the new layer conftests must carry what applies.
4. Quarantine bookkeeping: moved node ids gone from `quarantine.toml`; no test left deselected by an entry that now points nowhere.
5. Network-off proof: a deliberate outbound connection fails in the image both under `container` and Docker.

---

### Task 1: Prune tests the refactor made obsolete

**Files:** any `tests/unit/test_*.py` with dead tests; `tests/unit/quarantine.toml`; report `docs/superpowers/plans/2026-11-09-test-suite-by-layer-prune.md` (a table: node id, verdict delete|fix, reason).

- [ ] Run the whole unit suite on this branch's base including the quarantined tests: `uv run pytest -q -n auto --maxprocesses 8 tests/unit --quarantined include -p no:randomly` and record every failure and error.
- [ ] Find dead tests that still pass: tests whose subject module/symbol is gone or only reachable through a shim (search `src/` for what each suspicious test imports; #270 removed vllm-mlx and llama-server, #261/#263 replaced the workflow schema).
- [ ] Sort each by the spec's delete rule: delete when what it pins is gone; fix when the behaviour survived in a new shape (a renamed name, a moved module, a changed signature).
- [ ] Apply: delete or fix; drop deleted node ids from `quarantine.toml`.
- [ ] Re-run the merge of the triage on what remains and update the counts in the prune report.
- [ ] Gate; commit `tests: prune the tests the engine swap and the workflow refactor made obsolete`.

### Task 2: Linux test image and layer tasks

**Files:** create `ops/test/Containerfile`; modify `Taskfile.yml` (`test:functional`, `test:contract`, `test:integration`, `test:unit:image`, `test:mac`), `scripts/run_layer.py` (an `--in-image` path or a wrapper task), `.github/workflows/pipeline.yml` (only the unit/functional/contract/integration jobs: build the image, run the layer in it with `--network none`), `pyproject.toml` (register the `mac` marker), `tests/unit/conftest.py` (skip `mac` tests where mlx/sandbox-exec are missing; keep MAC_ONLY behaviour), create `tests/integration/test_image_network.py` (proof test, below).

**Interfaces:**
- `task test:<layer>` (functional, contract, integration) builds `airlie-test` once (cached layers) and runs `scripts/run_layer.py <layer>` inside it with the checkout mounted read-only at `/src`, the venv at `/opt/venv`, tmp on the container's own filesystem, outside network off.
- `task test:unit` stays on the host (the local gate); `task test:unit:image` runs unit in the image (what CI runs).
- `task test:mac` runs the `mac`-marked tests on the host.

- [ ] Proof test first: `tests/integration/test_image_network.py::test_an_outbound_connection_fails_in_the_image` — opens a TCP connection to `1.1.1.1:443` with a 2 s timeout and asserts it fails; `test_loopback_still_works` — binds `127.0.0.1:0`, connects, round-trips a byte. Mark the outbound test so it is skipped outside the image (env `AIRLIE_TEST_IMAGE=1` set by the Containerfile).
- [ ] Containerfile: base `python:3.12-slim-bookworm` (or the e2e base if git/node versions require it), uv pinned to the lockfile's version, git, node at `web/package.json`'s `engines` major (for any test that shells to node), ruff via the venv; `uv sync --frozen --no-install-project` skipping torch/CUDA as in `ops/e2e/Containerfile`; git identity set system-wide (`git config --system user.name airlie-test`, `user.email test@airlie.invalid`, `init.defaultBranch main`); no `insteadOf` rules.
- [ ] Network off: Docker `--network none`; Apple `container` 1.5 — find the equivalent (an internal network with no route out, or `--network none` if supported) and use it; document which in a comment.
- [ ] Wire the Taskfile tasks and the four CI jobs; CI builds the image once per job (or shares it with `docker save` artifacts if simple).
- [ ] Check: `task test:integration` runs the proof tests green in the image (outbound fails, loopback works); `task test:unit:image` passes with `mac` tests skipped; empty layers still pass.
- [ ] Gate; commit `tests: one Linux image for the layers, outside network off; a mac marker for what needs the Mac`.

### Task 3: `tests/support/` package

**Files:** create `tests/support/__init__.py` and modules; modify every test that imports a helper from another test module.

**Interfaces:** `tests.support.git` (today's `tests/unit/git_helpers.py` and `repo_with_marker_tests` from `test_fork_orchestrator.py`), `tests.support.story` (`pipeline()`, `RecordedGh`, the story fakes from `test_story_pipeline.py`; `repo()` from `test_story_layer_checks.py`; `calc_repo`/`run` from `test_story_layer_checks_flow.py`; `TickRig` from `test_story_tick_epics.py`), `tests.support.engines` (fake engines/scripted replies used across files), plus any other cross-module helper the import scan finds.

- [ ] Scan: list every `from tests.unit.test_* import` / `from .test_* import` / `import tests.unit.test_*` (and conftest-provided helpers imported by name).
- [ ] Move each helper unchanged (same names and signatures); leave no re-export shims in test modules.
- [ ] Check: `grep -rnE "from (tests\.unit\.)?\.?test_[a-z_]+ import" tests` returns nothing; the full suite passes with the same counts as before the move.
- [ ] Gate; commit `tests: shared helpers live in tests/support, not in test modules`.

### Task 4: Move the whole files to their layers

**Files:** `git mv` the 39 files (as re-checked after Task 1) to `tests/integration/`, `tests/functional/`, `tests/contract/`; create `tests/functional/{__init__.py,conftest.py}`, `tests/contract/{__init__.py,conftest.py}`; extend `tests/integration/conftest.py`; remove moved node ids from `tests/unit/quarantine.toml`; fixture data paths updated (`tests/fixtures` stays shared).

- [ ] For each destination conftest: carry the unit conftest's applicable autouse fixtures (env scrubbing, ATLASSIAN scrub, `_no_mutation_harvest` where Line/factory beats run), the per-test timeout, and nothing else.
- [ ] Move; fix imports to `tests.support`; delete their quarantine lines.
- [ ] Check (Review Focus 1): for each layer, collected count = number of node ids moved in (record before/after with `--collect-only -q`); `task test:functional`, `task test:contract`, `task test:integration` green in the image; unit on the host green with `AIRLIE_TEST_BUDGET_MS=500`.
- [ ] Gate; commit `tests: integration, functional and contract files leave the unit layer`.

### After Task 4

Open the PR (`tests: every layer runs in a networkless Linux image; non-unit files leave tests/unit`) with: the prune table, the moved-file table (file → layer, node ids, quarantine lines removed), the network proof, and the four layer counts. Merge only when every CI job, e2e included, has finished green.
