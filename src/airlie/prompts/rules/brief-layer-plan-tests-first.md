---
kind: rule
roles: [coder]
fields: [criteria]
governs:
  tests.order: first
  tests.criteria: each
  tests.fail-first: required
why: |
  A layer with no tester that owns criteria: the coder writes their tests first and sees them fail. No
  docstring in the code; test-first layer plans since b34f992 (2026-10-14), worded as now since 28107dd
  (2026-10-16: one coder writes a layer's tests and code, and the pipeline checks them with no model).
  `criteria` is the layer's criteria, one "- " line each.
---
Write the tests first: at least one test proving each of these acceptance criteria, where this repository keeps its tests:
{criteria}
Run run_tests and see them fail because the feature is missing, not because a test is broken. Then write the code that makes them pass, without weakening them.
