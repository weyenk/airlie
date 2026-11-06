---
kind: steering
roles: [architect]
when: A layer's package failed the readiness check (parse, lint, imports, covers, red on the base) and the architect is asked for the whole package again; it closes the retry, after the findings.
governs:
  package.fix: whole-reply
why: |
  What the architect is told to do with the readiness check's findings, the last thing it reads on a package
  retry (P1 of the planning line, 2026-10-24). It replaces the tester's `checks-fix-before-code`: the same
  rule that a test must fail for the right reason before the code exists, now said to the role that writes
  the tests, and the reply is the whole package again because the pipeline writes the tests from the reply
  and nothing else. The guard was added 2026-11-06 (AIR-61 layer 02: a criterion already true on main).
---
Reply with the whole package again: every test file in full and the JSON object, with these findings fixed. A test that passes before the code does not test this layer: make it fail for the right reason, mark it a regression guard when what it checks already holds on the base, or drop it. Do not write the feature's code.
