---
kind: steering
roles: [coder]
when: The static checks found something in the tests a coder with no tester wrote with its code, and the coder gets its one retry; it closes the retry, after the findings and the supervisor's advice, if any.
governs:
  tests.findings: fix
  tests.criteria: each
  done.when: tests-pass
why: |
  What a coder that wrote a layer's tests and code together is told to do with the findings (`feedback`
  without `before_code`). No docstring in the code; added with the post-hoc checks (28107dd, 2026-10-16:
  one coder per layer, its new tests read for assertions with no model).
---
Fix each one: assert the behaviour this layer adds. Keep the tests for the acceptance criteria. Run run_tests until your tests pass, then stop.
