---
kind: rule
roles: [coder]
governs:
  scope: layer
  done.when: tests-pass
  answer.form: summary
why: |
  The close of every build brief: the layer's scope, and when to stop. No docstring in the code; since the
  story briefs were written (b34f992, 2026-10-14), done when the layer's tests pass since 1741541
  (2026-10-19: the coder's run_tests runs only the layer's tests, and the whole suite runs after review).
---
Stay inside this layer's scope. Run run_tests until the layer's tests pass, then stop with two or three sentences on what you changed.
