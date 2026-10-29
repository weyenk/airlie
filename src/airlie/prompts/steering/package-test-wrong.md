---
kind: steering
roles: [architect]
when: A layer's gate is red because the supervisor diagnosed a test the coder may not change as wrong (wrong_acceptance_test); said before package-replan.
governs:
  replan.cause: wrong-test
why: |
  Roman, 2026-10-28: twice in three runs a criterion carried a wrong example ('MCMXCIV' for 1984,
  'MMMDCCCLXXXIX' for 3999); the tests locked it, a coder gamed it once and spent a 31-minute fix round on it
  the next time. The coder now stops and the layer comes straight back here with the diagnosis.
---
The coder stopped because a test it may not change asserts what correct code cannot give, and the supervisor agreed (its diagnosis is above). The criterion's example was wrong, not the coder: correct that test to what is actually right, keep every other test as it was, and do not weaken a test to make it pass.
