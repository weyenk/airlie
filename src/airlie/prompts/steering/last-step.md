---
kind: steering
roles: [any]
fields: [n]
when: The role has used all max_steps of its steps; the turn after offers no tools and asks for the final answer.
governs:
  answer.when: now
why: |
  What a role is told on the turn after its last allowed step, which offers it no tools.
---
You have used all {n} of your steps: no more tools. Give your final answer now, complete, from what you have already found.
