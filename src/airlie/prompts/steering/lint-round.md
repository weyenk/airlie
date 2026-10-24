---
kind: steering
roles: [tester]
when: All that is left after the tester's pre-code retry is lint its fixer could not fix; the tester is sent back once more, with only ruff's output and a few steps.
governs:
  tests.findings: fix
  feature.code: never
why: |
  The lint round (`tester_lint.lint_round_said`): after the one retry, a lint error alone used to block the
  layer, and the coder cannot edit the tester's files to fix it. AIR-22 layer 03 (2026-10-24) lost a layer
  of about 2.5 hours to E741, a loop variable named `l`, which ruff's fixer cannot rename. The tests
  themselves held by then, so the tester is told to change only the lines ruff names.
---
Your tests hold, but the gate's linter still fails on your files, and airlie's fixer cannot fix what is left. Change only the lines ruff names above, as each rule asks (rename the variable, shorten the line), and nothing else: do not rewrite, add or remove tests, and do not write the feature. Then stop.
