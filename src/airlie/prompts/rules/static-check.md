---
kind: rule
roles: [coder]
governs:
  tests.assert: always
why: |
  What a coder with no tester before it is told of the check its tests meet (static_findings).

  Added with the pre-code check (1741541, 2026-10-19), for the layer whose coder writes its tests and code
  in one subtask, so its tests can only be read, not run before the code.
---
In a pytest repository airlie reads each test you add or change: one that does not parse, or has no assertion, is sent back to you.
