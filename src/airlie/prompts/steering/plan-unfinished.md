---
kind: steering
roles: [architect]
when: The architect's round 1 ends with no answer (its steps ran out before it wrote a plan); it is asked for the plan in the same conversation, within the fixes allowed.
governs:
  plan.form: json
  reading: stop
  answer.when: now
yields_to:
  - plan-round
why: |
  What the architect is told when its first round ended with no answer: sent back in the same conversation,
  which has read the code, instead of asked again from the start (AIR-30, 2026-10-19: two fresh starts each
  read five files and stopped with no plan).
---
You ran out of steps before you wrote your plan. Write the whole plan now, in the JSON the brief asks for, from what you have already read. Read more only if you cannot plan without it.
