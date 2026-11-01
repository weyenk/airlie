---
kind: rule
roles: [coder]
governs:
  lint.fixes: automatic
why: |
  AIR-48 layer 1 (2026-11-01): the coder was shown the whole gate twice (the repository map's test command and
  AGENTS.md's Test gate) and never told that airlie already applies ruff's fixes before each run_tests
  (autofix_steps, tools/scope.py).
---
`run_tests` runs the repository's whole gate with pytest limited to your test files. Before it runs, `ruff format` and ruff's fixable lint are applied for you: do not spend steps on formatting or on a lint ruff can fix. Read what it still reports.
