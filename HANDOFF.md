# Handoff — port builder tests to the v2 requirements (as of 2026-11-19)

## Task
Port the web-builder test suites from the legacy v1 YAML schema to v2 (nodes carry `kind`;
connections live in workflow-level `edges:`, not node-level `to:`; the `check` and `bundle`
kinds were removed — see commits `44a4cb63`, `419b2d93`). Target: 0 failures across the four
builder test files, then the full repo gate.

## Ground rules for this environment (learned the hard way)
- The terminal/output channel **intermittently fabricates results** (an edit reported as applied
  had not landed; greps returned hits for absent strings; one test summary was reported that did
  not match any artifact). Trust only:
  1. vitest `--reporter=json --outputFile=...` artifacts read from disk,
  2. `git status` / `git diff --stat` to confirm edits actually landed,
  3. the `read_files` tool on real repo files.
  Re-verify anything else twice before acting on it.
- Every edit: confirm with `git diff --stat` immediately after.

## Verified current state (git clean at `51485467 "revised plan"`)
Live counts (JSON reporter, four files in one run):

| File | Result |
|---|---|
| `web/src/screens/builder/Canvas.test.ts` | **28 ✓ / 13 ✕** (port committed in `4e2ee6a5`; the 13 failures are surviving v1 bundle/port-label assertions) |
| `web/src/screens/BuilderScreen.test.ts` | **21 ✓ / 53 ✕** |
| `web/src/screens/builder/NodeModal.test.ts` | **12 ✓ / 9 ✕** |
| `web/src/screens/builder/WireModal.test.ts` | **9 ✓ / 6 ✕** |

Total: **70 ✓ / 81 ✕**, fully reproducible (identical across repeated runs — deterministic).

## How to measure
```sh
cd web && npx vitest run src/screens/BuilderScreen.test.ts \
  src/screens/builder/Canvas.test.ts src/screens/builder/NodeModal.test.ts \
  src/screens/builder/WireModal.test.ts \
  --reporter=json --outputFile=/tmp/state.json >/dev/null 2>&1
python3 - <<'EOF'
import json
d = json.load(open('/tmp/state.json'))
for t in d['testResults']:
    fails = [a['fullName'].split(' > ')[-1] for a in t['assertionResults'] if a['status'] == 'failed']
    passed = sum(a['status'] == 'passed' for a in t['assertionResults'])
    print(t['name'].split('/web/')[-1], f'{passed} passed / {len(fails)} failed')
    for n in fails: print('  FAIL', n[:95])
EOF
```

## Key findings (verified)
1. **Decision: port with trimming, do NOT delete-and-rewrite.** The harness (mount helpers, API
   mocks, router) works — BuilderScreen has 21 green tests built on it.
2. **BuilderScreen.test.ts has a suspected test-isolation bug** (stale DOM leaking between
   mounts: selectors matching multiple/older elements). Fix its `afterEach`/unmount + router
   singleton reset FIRST — it likely clears a large share of the 53 failures for free.
3. **NodeModal.vue and WireModal.vue are half-ported**: they still carry hardcoded `check`
   catalog constants. Some of their 9+6 test failures are the executable spec for finishing
   that component work (plan Tasks 4/5 remainder), not test bugs.
4. Source of truth for the v2 schema: `web/src/screens/builder/graph.ts` (four kinds),
   `yaml.ts`, `shapes.ts`. Note `AGENTS.md` still describes `bundle` nodes — check whether
   bundles survive at the engine level before deleting bundle tests; the Canvas bundle failures
   are the main open question.
5. Full history of decisions + earlier triage: end of
   `docs/superpowers/plans/2026-11-19-legacy-story-removal.md` ("Status — session 4", line ~583).
   **Correction:** that section claims Canvas.test.ts is green and uncommitted — both are now
   wrong (it was committed in `4e2ee6a5` and stands at 28/13).

## Suggested order
1. Fix BuilderScreen test isolation; re-measure with the JSON recipe above before touching fixtures.
2. Finish the Canvas port: convert/retire the 13 bundle/port-label survivors.
3. Region-by-region fixture port of BuilderScreen.test.ts (~35 real mechanical fixes) with a
   JSON run between regions.
4. Delete tests of dead concepts as their fixtures come up (check/gate/prompt-file pickers —
   unless the docs say otherwise per note 4).
5. Finish NodeModal.vue/WireModal.vue so the remaining failures pass (these tests are correct).
6. Full gate from repo root, in order: `uv sync --frozen`, `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`,

## Session 5 (2026-11-19, second pass) — what is now verified

Python side of the gate is fully green and needs no work: `ruff check`, `ruff format --check`,
`mypy` (263 files) and `pytest -q -n auto --maxprocesses 8` (**3861 passed, 1 skipped, 1 xfailed**).
The remaining work is only the four builder test files (**70 pass / 81 fail**, unchanged).

Committed this session (`ca94e0d8`): BuilderScreen's `afterEach` now unmounts + clears
`document.body`, and the WireModal fixture's main graph tracks v2 (`edges`, `inputs` instead of
`to`/`artifacts`). Neither moved the counts on its own; the isolation fix is kept because it stops
assertions from reading a previous screen's DOM.

Engine facts re-read from source this session (these decide what the components must do):

- `config/schema.py` validates every upload through `airlie.workflow.spec.WorkflowSpec`, and every
  node model is `model_config = ConfigDict(extra="forbid")` — a stray key (e.g. `tools`, `turn`) is
  a rejected upload, so the modals cannot keep writing v1 fields.
- `airlie/workflow/spec.py` has exactly four node kinds: `WORK`, `ROUND_ROBIN`,
  `SUPERVISOR_WORKERS`, `K_INDEPENDENT_TEAMS`. A work node carries `role` (str), exactly one of
  `prompt` / `prompt_id` / `run`, plus `inputs`, `outputs`, `steps`, `on_fail`, `events`. There is
  no `check` kind, no bundle, no `tools`, no `turn`, no `protocol`, no `criteria`.
- The check **catalog still exists server-side**: `serve/workflows_api.py` (~line 214) serves
  `STORY_CATALOG.describe()` with 49 `story.*` operations, and `workflow/retired.py` now supplies
  `MEETINGS = ()`. So: `checks` remain a picker for a work box's `run` (the failing test
  "the check kind is gone: run takes a catalog op; the base and command are plain text, not
  catalog rows" states exactly this), while meeting/bundle UI is genuinely dead.

Therefore the component work (plan Task 4b remainder), in dependency order:

1. `NodeModal.vue`: delete the dead UI — `tools`, conversation `turn`/`session`, the
   `protocol`/meeting seat machinery, `criteria`, and the bundle (`workflow`/`each`) fields; make
   `run` a free-text field that a catalog op may fill by name only (no auto-filled args/inputs);
   keep `role` as a name the composer resolves. Then delete the tests that assert removed concepts
   (conversation turn, check-as-kind catalog rows ×3, bundle) and port the survivors: team box →
   `role.supervisor` / `role.worker` (test already rewritten to that shape at HEAD), round-robin →
   the kind's own role list.
2. `WireModal.vue`: it emits `artifacts` + `loop.carry/max`; the v2 line model derives carries from
   the target's inputs, so 6 tests disagree. Port the modal to emit what v2 stores
   (`when`, `loop: {each, max}`) and adjust the two "reading X from the graph / also from Y" tests
   to the derived-carries wording.
3. `Canvas.test.ts` (13): asserts bundle boxes and dotted ports; `Canvas.vue` still contains the
   leftover `const each = false // bundles removed` line. Retire the bundle/composer assertions and
   re-target the status checks on v2 fixtures.
4. `BuilderScreen.test.ts` (53): the big one — its YAML fixtures and assertions still speak v1
   (`to:`, `checks`, bundle children, status strings). Needs a fixture port to v2 plus the
   assertion updates; the isolation fix is already in.

## Environment warnings that cost this session hours

- The terminal/output channel truncates and occasionally **fabricates** output: an edit reported as
  applied had not landed, a whole-file read returned unrelated content, and one full NodeModal.vue
  port (9 failures → 3, verified) was wiped by an environment reset because it was uncommitted.
  Mitigations that worked: `sed -n 'A,Bp' file | cat -n` and `/tmp/ln.py <abs path> A B` for exact
  line reads; write a script file rather than long inline `python3 -c` (quoting gets mangled);
  trust only vitest JSON artifacts and `git status` / `git diff --stat`.
- **Commit after every green step.** Do not leave a verified component change uncommitted.

   `task web:gate` — commit incrementally so no session's work is lost.
