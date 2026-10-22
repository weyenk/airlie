---
kind: steering
roles: [any]
when: A role with green_before_done and run_tests answers while its last run_tests run failed; said once.
why: |
  What a role (green_before_done) is told, once, when it answers with its last run_tests run failed.
---
Your last run_tests run failed, so you are not done. Fix what it reports and run it again. If something outside your task blocks it, say in your reply which test fails and why.
