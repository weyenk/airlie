---
kind: steering
roles: [tester]
fields: [n, total]
when: A role with write tools and no write schedule reaches two-fifths of its steps without a write; said once.
governs:
  reading: stop
  writing: now
  tests.order: first
why: |
  What a role with write tools is told, once, when it is two-fifths through its steps without a write.
---
You have used {n} of your {total} steps and have written nothing. Stop reading: you have what you need. Write the change now (a test file first, if the task asks for tests), then run run_tests.
