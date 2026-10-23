---
kind: steering
roles: [any]
when: A role with no tools replies with a tool call; a second such reply in a row ends the run (no_tools).
governs:
  answer.when: now
why: |
  What a role without tools is told after one reply that calls a tool anyway; a second in a row ends the run
  (no_tools).
---
You have no tools in this task, so nothing you call can run. Answer directly, in your reply, with the complete deliverable.
