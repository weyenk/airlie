---
kind: steering
roles: [any]
fields: [n, total]
when: A role with a write schedule, in a continuation (a fix round, a follow-up), reaches its one deadline without a write of any kind this round; said once.
governs:
  writing: now
why: |
  What a role with a write schedule is told, once, in a continuation (a fix round, a follow-up) at its one
  deadline without a write of any kind this round.
---
You have used {n} of your {total} steps in this round and changed nothing: what you were sent back for is still there. Make the change now from what you have read, then run run_tests.
