---
kind: steering
roles: [any]
when: The final test gate fails after the subtask named by gate_follow_up finished; the gate's output follows it, once, before the gate runs again.
why: |
  What the subtask named by execute_task's `gate_follow_up` is told when the final gate fails.
---
Your tests pass, but the whole suite fails with your change: the output is below. Fix the code so the whole suite passes; the acceptance tests stay as they are. Run run_tests, then stop.
