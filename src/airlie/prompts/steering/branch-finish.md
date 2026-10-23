---
kind: steering
roles: [any]
when: Every fork branch's nudge ends with it.
governs:
  done.when: tests-pass
yields_to: [checks-fix-before-code]
why: |
  The last sentence of every fork branch's nudge since fork diversification was built (feat(fork),
  2026-10-13, aa2ee3e): what to do once the branch has its new start.
---
Make the change, run the tests, and stop with a short summary when they pass.
