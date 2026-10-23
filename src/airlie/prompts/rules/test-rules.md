---
kind: rule
roles: [tester, coder]
governs:
  tests.target: behaviour
  tests.assert: always
  tests.scope: one-behaviour
why: |
  How a layer's tests are written: the useful part of skills/qa.

  Added when one coder began to write a layer's tests and code (28107dd, 2026-10-16: the qa subtask kept
  blocking HDS-5 and HDS-11 at layer 01 with assertion-free tests); the tester's brief carries it too since
  the tester seat (3bda4e1, 2026-10-19).
---
What makes a test count:
- Test behaviour, not the implementation: call the code the way its users do and check what they observe
  (return values, files, output, errors), never private helpers or how it is built.
- Every test asserts: an assertion on the result, or a check that the expected error or warning is raised
  (in Python: `assert`, `pytest.raises`, `pytest.warns`). A test that only runs code proves nothing.
- One behaviour per test, named for it; the smallest setup that shows it; no sleeps, network or real clock.
