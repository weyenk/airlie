---
kind: rule
roles: [coder]
governs:
  gate-failure.fix: code
  tests.acceptance: unchanged
why: |
  The coder's contract in a build brief made from a package (P1, 2026-10-24). It replaces `tests-contract`:
  the tests were written by the architect before the coder, airlie has run them on the base, and they are
  locked by definition, so the coder cannot change what it was given. Its scoped run_tests is the layer's
  tests; the whole suite runs after review.
---
The tests for this layer were written before you by the architect and are in the worktree; airlie has checked that each fails without the code. They are the contract, and you cannot change them: follow the steps and make them pass. What else is in their files was there before (fakes, helpers, imports): change that where your change needs it. If a test is wrong about a name or a signature the steps fix, say which and why when you stop. Add tests in a new file for anything else you change. Your run_tests runs this layer's tests; once they pass, you are done: airlie runs the whole suite after review.
