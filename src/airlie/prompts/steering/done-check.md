---
kind: steering
roles: [any]
when: A coder stopped done on a brief with acceptance criteria and has a diff; it is sent this and the diff, before the review.
governs:
  self-check: criteria
  brief.wording: exact
why: |
  The done-check: a coder that stopped 'done' reads its own diff against the criteria before the reviewer
  does (AIR-31 layer 02, 2026-10-19: the reviewer's blocking finding was a value the brief had named).
---
Before review: check your change against the brief's acceptance criteria and its exact wording. For each criterion, quote the lines of the diff below that make it true, or say 'not met'. Where the brief names a value, a message, a field, or which value to compare, check the diff uses exactly that. Anything not met or not exact: fix it now, run run_tests, then stop. If everything is met, reply with the checklist and stop.
