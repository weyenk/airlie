---
kind: rule
roles: [tester]
governs:
  tests.fail-first: required
  tests.old: still-pass
  tests.assert: always
why: |
  What the tester is told of the check its tests meet (story.layer_checks).

  Added with the pre-code check (1741541, 2026-10-19: a layer is test-first in the TDD sense; each new test
  must fail on the whole suite before any code is written, and every other test pass).
---
In a pytest repository airlie runs the whole suite on your tests before the layer's code is written: each new test must fail there, or it does not test the layer, and every test that was there before must still pass. A test with no assertion is refused too.
