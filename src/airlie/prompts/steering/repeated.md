---
kind: steering
roles: [any]
fields: [n, tool]
when: A role with nudge_stuck makes the same call repeat_limit times in a row with the same result; said once.
why: |
  What a role (nudge_stuck) is told, once, when it repeats an action instead of being stopped.
---
You have made the same call {n} times in a row with the same result ({tool}). Repeating it changes nothing. Do something different: act on what you know, or give your answer.
