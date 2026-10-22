---
kind: steering
roles: [tester]
when: The pre-code check found something in the tester's tests and the tester gets its one retry; it closes the retry, after the findings and the supervisor's advice, if any.
governs:
  tests.findings: fix
  tests.fail-first: required
  tests.negative: inside-trigger
  tests.old: still-pass
  tests.criteria: each
  feature.code: never
  done.when: only-new-red
why: |
  What the tester is told to do with the findings, the last thing it reads on its retry (`feedback` with
  `before_code`). No docstring in the code; added with 1741541 (2026-10-19). The sentences on a test that
  checks nothing happens came with b7c2b75 (2026-10-19, AIR-31 layer 02: a test that a feature is a no-op
  below its threshold passed on the base whatever the tester did, and "make it fail" gave no way out).
---
Fix each one. Every test you wrote must fail now, because the code it tests is not written yet, and pass once the brief is built. A test that only checks that nothing happens (no event, no message, no change) passes now, where nothing happens either: put that check in the same test that triggers the behaviour, before the trigger, or drop it. Every test that was there before must still pass. Keep a test for each acceptance criterion. Do not write the feature. Run run_tests and check that your new tests are the only red, then stop.
