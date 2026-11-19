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
   `task web:gate` — commit incrementally so no session's work is lost.
