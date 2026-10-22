---
kind: steering
roles: [any]
fields: [n, total]
when: A role with a write schedule reaches its tests deadline without a test write; said once.
why: |
  What a role with a write schedule is told, once, at its tests deadline without a test write.
---
You have used {n} of your {total} steps and have written no test. Stop reading: you have what you need. Write the failing test now, then the code, then run run_tests.
