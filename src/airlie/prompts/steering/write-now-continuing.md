---
kind: steering
roles: [tester]
fields: [n, total]
when: A continuation (a retry or fix round) of a role with write tools and no write schedule reaches two-fifths of its steps without a change of its own; said once.
governs:
  reading: stop
  writing: now
  tests.order: first
why: |
  What a role sent back to change something is told, once, when it is two-fifths through its steps without a write of its own. A continuation counted an earlier run's write as its own and was never nudged (AIR-9 layer 05, 2026-10-23: the tester's retry read for all 20 steps and never wrote).
---
You have used {n} of your {total} steps on this round and changed nothing yet. You were sent back to change something: make that change now with edit_file or write_file, then run run_tests.
