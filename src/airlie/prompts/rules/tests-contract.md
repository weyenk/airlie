---
kind: rule
roles: [coder]
governs:
  tests.acceptance: unchanged
  tests.helpers: change-as-needed
  tests.extra-location: new-file
  tests.cover: changes
  done.when: tests-pass
why: |
  What the coder is told when a tester wrote the layer's tests before it. No docstring in the code; added
  with the tester seat (3bda4e1, 2026-10-19, lever 2: on AIR-31 layer 02 the coder had the code right by
  step 26, then spent 80 steps failing to write a test that crosses the threshold) and reworded when the
  tests became red before the code and read-only to the coder, whose run_tests runs only them (1741541,
  2026-10-19). Reworded again when the lock moved from whole files to the tester's definitions (AIR-9 layer
  05, 2026-10-22: a whole-file lock kept the coder off a fake in the test file that the signature change
  needed, and the layer blocked): the tests stay the contract, what was in their files before is the coder's.
---
The tests for this layer were written before you (the result of 'tests' below lists them and what each asserts), and airlie has checked that each fails without the code. They are the contract, and you cannot change them: make them pass with the code. What else is in their files was there before them (fakes, helpers, imports): change that where your change needs it, a signature a fake copies say. If a test is wrong about a name or a signature the brief fixes, say which and why when you stop. Add tests in a new file for anything else you change. Your run_tests runs your tests; once they pass, you are done: airlie runs the whole suite after review.
