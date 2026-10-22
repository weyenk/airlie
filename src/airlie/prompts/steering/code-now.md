---
kind: steering
roles: [any]
fields: [n, total]
when: A role with a write schedule reaches its code deadline without a code write; said once.
why: |
  What a role with a write schedule is told, once, at its code deadline without a code write.
---
You have used {n} of your {total} steps and have written no code. Stop reading: make the change now from what you have read, then run run_tests.
