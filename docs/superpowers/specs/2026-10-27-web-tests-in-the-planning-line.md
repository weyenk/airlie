# Web tests in the planning line (Vitest)

Status: built 2026-10-27 (Noah approved it the same day): js_tests.py, the package check, the proof and texts.

## Why

The wiki benchmark's run B (2026-10-27) blocked at the end of the story meeting: its two Vue layers had no test in the
proof plan. The tester proposed `web/src/screens/Wiki.test.ts`, where airlie keeps its 68 Vitest tests, and the proof
refused it: a test file must be a pytest module under `tests/<level>/`. The architect saw the bind and said so ("no
named test can point at them"), but no split satisfies it. P1 and P2 know Python tests only: the proof's levels come
from `tests/`, a package's test files must be pytest modules, test names are found with Python's `ast`, and the red
check runs pytest. So no story whose layers live in the Vue app can be planned. Noah chose to support Vitest properly
rather than prove such layers by the gate alone.

## What changes

- **D1. Test levels.** `test_levels` adds one level per top-level directory that holds JS/TS test files
  (`*.test.ts`, `*.spec.ts`, `.js` alike, `node_modules` never), named by that directory: airlie gets `web` beside
  `unit`, `integration`, `e2e`, `live`. The conventions text says how a level runs (pytest, or Vitest through
  `npm test` in that directory).
- **D2. Test files.** One predicate for "a test file": a pytest module or a JS/TS test file. The proof's file rule
  for a JS level is "under `<level>/`". A package may carry `.test.ts` files; they are placed and locked like any
  other.
- **D3. Test names.** A Vitest test is an `it(...)` or `test(...)` call with a literal title; inside
  `describe("X", ...)` its name is `X > title`, as Vitest reports it. The ids are `file::name`, as for pytest.
  Titles are found with a small parser for string literals and bracket nesting: no TypeScript toolchain in
  Python. A title that is not a literal (a template or a variable) is a finding.
- **D4. The package check for a TS test file.** The architect's TS tests get the same checks a Python test gets:
  - each covered id names a test in its file
  - each test has an `expect(...)`
  - the file is new or changed by the package
  - **red before the code:** the package's tests are run with Vitest on the base (`npx vitest run <file> -t
    <title>`) and must fail, and the level's existing tests must still pass
- **D5. Running Vitest in the scratch worktree.** The red check needs `node_modules`. `npm ci` in every scratch tree
  costs about a minute. When the repository's own `<level>/node_modules` exists and its `package-lock.json` matches
  the base's, it is linked in instead. Node is the one first on PATH, and the doctor says when it is older than the
  repository's `engines`.
- **D6. Out of scope.** Ruff lint and import resolution stay Python-only; the gate's ESLint and typecheck cover TS.
  Component tests (`.vue` mounted in jsdom) are ordinary Vitest files to all of this.

## Contracts and texts

- `PROOF_CONTRACT`: a test's level may be a JS level; its file is under that directory; its name is the test's
  title, or `describe > title`.
- `amigos-tester` and `package-tests-rules`: write a Vue layer's tests as Vitest tests under its level, in the
  repository's existing style (read the nearest `*.test.ts` first).

## Done when

- A story whose layer is in `web/` gets an agreed proof with Vitest tests, a package whose TS tests pass the
  checks, a red run before the code, and a build whose gate runs them.
- Unit tests cover every rule above, and the wiki benchmark's run B is rerun on it.
