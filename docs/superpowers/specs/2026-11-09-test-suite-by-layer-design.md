# Test suite by layer: design

2026-11-09. Approved by Noah after review in the Claude Doc https://claude.ai/artifact/LDmpLTS2Aq4jmhNYE5GxP6
(that doc is the record of the discussion; this file is its text at approval).

## What was found

About half of `tests/unit` is not unit tests by the project's own standard, so the fix is to put every test in the
layer it belongs to and make each layer obey its guideline.

- **The standard** is `skills/qa/unit.md` (vendored from ai-mode): one unit in isolation, with databases, HTTP,
  clocks and file I/O replaced by doubles, and FIRST (fast, independent, repeatable with time frozen). Siblings in
  `skills/qa/` define integration, functional, contract, component and e2e.
- **The triage** of all 204 files in `tests/unit` against it:

| Verdict | Files |
| --- | --- |
| Clean unit | 93 |
| Whole file belongs in integration | 24 |
| Whole file belongs in functional | 11 |
| Whole file belongs in contract | 4 |
| Mixed, split by test | 72 |

- **Why they fail unit:** real git repos built per test (helpers in `test_fork_orchestrator.py`, `git_helpers.py`,
  `test_story_pipeline.py`, about 40 files), real SQLite stores (16 files), real child processes (sandbox-exec,
  ruff, pytest, shell, a uvicorn socket), real network (publish's `git ls-remote`), and real sleeps or timeouts (a
  5 s engine retry pause alone costs 400 s).
- **Today's stopgap,** PR #271's quarantine, deselects 559 slow or clock tests. Main's unit job has been red since
  #270 because tests sitting at the 500 ms line cross it at random on CI.
- **Where tests run:** in CI every layer runs on Ubuntu VMs except the mlx tests (one file plus three tests) and
  the macOS-sandbox tests. Locally everything except e2e runs on the Mac host. The functional, contract and journey
  directories do not exist, so those CI jobs pass as "no tests yet".
- **Agents write tests too.** airlie's tester and coder roles, and the coding agents that work on airlie, add tests
  every day, so the fix has to hold for them, not only for today's files.

## Decisions

Every test lives in the directory of its layer, each layer is checked mechanically against its guideline, and
almost all of it runs in one Linux image locally and in CI.

1. **One directory per layer.** `tests/unit`, `tests/functional`, `tests/contract`, `tests/integration`,
   `tests/e2e`, plus `tests/live` (needs a running engine, never in CI). The layer is decided by the
   `skills/qa/*.md` definitions, test by test, not by file name. CI already runs one job per directory through
   `scripts/run_layer.py`.
2. **A unit guard enforces the rules.** A plugin in `tests/unit/conftest.py` fails any unit test that starts a child
   process, opens a socket, sleeps for real (any non-zero `time.sleep` or `asyncio.sleep`), or opens a SQLite file.
   That makes the guideline a check, not a hope, for people and agents alike. It runs first in report mode to
   confirm the triage, then in enforce mode.
3. **Shared test support moves out of test modules.** Helpers now imported from test files (`pipeline()`,
   `RecordedGh`, `repo_with_marker_tests`, `git()`, fake engines) move to a `tests/support/` package with fakes and
   builders for unit and real-collaborator rigs for integration, so a layer imports only what it may use.
4. **The clock is injected.** Code that waits or reads the time takes a clock (or reads one module-level seam) that
   unit tests replace. The engine retry pause, poll loops, heartbeats, revoke and gate timeouts, and the board's
   7-day archive all get a seam.
5. **The quarantine retires.** Once slow tests have moved to their layers and clock tests are fixed,
   `quarantine.toml` and its plugin are deleted. The 500 ms budget stays on unit only; the other layers get a
   per-test timeout instead of a budget.
6. **One Linux test image.** A `ops/test/Containerfile` image holds Python 3.12, uv, git, ruff and node.
   `task test:<layer>` runs inside it, under Apple's `container` locally and Docker in CI, as e2e already does. The
   local AGENTS.md gate runs unit on the host for speed, and functional, contract and integration through the
   image; CI runs all four in the image. In the image those layers run with networking off: dependencies are baked
   in at build time, so a test that reaches outside the container fails on its first run. Loopback stays on, so
   local servers and sockets still work. A service an integration or contract test needs runs in the container or
   on an internal network with no route out. Contract tests check the engine's wire format against a local stub or
   pinned fixtures. Checking it against a real remote engine belongs in tests/live.
7. **A small Mac-only remainder.** Tests that need mlx or macOS `sandbox-exec` carry a `mac` marker and run on the
   host and on CI's macOS job. Everything else must pass in the Linux image. CI's macOS job is provisional: once the
   prune and split show how many mac tests remain, a small set may run from a git hook on the Mac instead.
8. **Agents follow the same guidelines.** airlie's tester and architect prompts cite the layer guidelines and say
   where a test goes. The package and layer checks reject a test placed under `tests/unit` that the unit guard would
   fail. AGENTS.md tells coding agents the same, so Claude and Codex sessions on airlie follow it. Prompts only
   inform; checks enforce. Every layer's conftest guards what that layer may touch, and an import rule keeps unit
   off the real-collaborator rigs. The layer checks are a required CI status, and CODEOWNERS on the guards stops an
   agent weakening them in its own PR. The amigos meeting writes a test plan into the story as a reviewable
   artifact: each acceptance criterion, its layer, and the tests that will cover it. The plan is approved before the
   tester starts, and the package check compares the tester's tests against it. In other repositories (OpenHand,
   lhr) these layers are the default, and the target repository's stated conventions win where they differ.

## Relocate, rewrite or delete

Isolation comes first: a test is rewritten to a unit test with fakes unless the real collaborator is the point.
Only then does it move as written, and it is deleted when the refactor removed what it pins. Isolated tests are
faster, easier to debug and stable, so an integration failure points at real wiring.

- **Relocate** when the test checks that airlie works with the real thing: git worktrees, the sandbox, SQLite
  queries, the FastAPI app across routes, the engine's wire format. Keep one such test per real path; the rules
  along it are pinned in unit. It moves to integration, functional or contract with its body intact, apart from
  importing helpers from `tests/support/`.
- **Rewrite to unit** when the assertion is about airlie's own logic and the real thing only sets the stage.
  Examples: a decision taken from a repo's state, where a fake repo state would do; a refine or plan parser fed
  through the whole pipeline; a board rule tested through a SQLite file. The rewrite keeps the behaviour the old
  test pinned, and the old test is deleted only when another layer covers the real path.
- **Keep both** when a behaviour needs its logic pinned and its wiring proven: a unit test for the rule, and one
  integration test for the path.
- **Delete when obsolete** after the refactor: the test imports a module or symbol that no longer exists, asserts
  behaviour that was removed on purpose, or covers only a shim or path no current code calls. A test that fails
  only because a name, signature or location changed is updated, not deleted.
- **Never drop coverage silently.** Each move, rewrite or delete PR lists the old node ids and where each went, or
  why it was deleted, and the mutation gate runs on the touched modules.

## Plan

Eleven tasks in four PRs, ordered so main's unit job goes green in the first PR and each PR leaves every layer
passing. The task-by-task plan is `docs/superpowers/plans/2026-11-09-test-suite-by-layer.md`.

- **PR 1: layers that run, main green.** 1 prune obsolete tests; 2 Linux test image and layer tasks (networking
  off, a `mac` marker); 3 `tests/support/` package; 4 move the 39 whole files.
- **PR 2: the split.** 5 unit guard in report mode; 6 split the 72 mixed files.
- **PR 3: clock and rewrites.** 7 clock seams and the 61 clock tests; 8 rewrites to unit, starting with the 24
  integration files cut to one test per real path.
- **PR 4: enforcement and agents.** 9 guard in enforce mode, the quarantine retires, per-layer timeouts; 10 agents
  (amigos test plan as a story artifact, prompts, package and layer checks, CODEOWNERS, AGENTS.md); 11 gate and docs.

## Calls

- **SQLite counts as I/O,** so store tests go to integration (board rules move to unit with a fake store; real
  queries stay in integration).
- **Serve tests that drive the app in-process** (`TestClient` with mocked transports) are functional.
- **PR #276 is closed unmerged.** PR 1 makes main green by moving the tests rather than listing more of them.
- **The unit budget stays at 500 ms,** per #271. Other layers get a per-test timeout of 60 s (integration) and 30 s
  (functional, contract).
- **Codex and other coding agents** get the rules through AGENTS.md, which they already read.
- **Tests that use the network today** (publish's `git ls-remote`) move to integration against a local bare repo,
  or to `tests/live` if they need a real remote.
- **Kept from the mutation work:** the `_no_mutation_harvest` autouse fixture stays in the unit conftest, and
  `scripts/mutate_changed.py` SETUP_FILES follows `tests/unit/conftest.py` if it ever moves.

Open: after PR 2, how many `mac`-marked tests remain, and is that small enough to drop CI's macOS job for a git hook?
