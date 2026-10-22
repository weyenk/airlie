---
kind: steering
roles: [tester, coder]
when: The checks on a layer's tests found something and the tests' author gets its one retry; this heads the list of findings, each test and why.
why: |
  Heads the findings the tests' author is told on its retry (`layer_checks.feedback`): the tester's after
  the pre-code check, a coder's with no tester after the static checks. No docstring in the code; added with
  1741541 (2026-10-19: a layer's tests must fail before any code is written, and findings go back to the
  tester once).
---
airlie checked the tests, and these need fixing:
