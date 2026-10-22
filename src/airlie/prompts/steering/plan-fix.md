---
kind: steering
roles: [architect]
fields: [error]
when: The architect's round-1 plan is refused by the parser; it goes back in the same conversation with the reason, up to the fixes allowed.
governs:
  plan.form: json
  reading: stop
why: |
  What the architect is told when its first plan is refused: sent back in the same conversation, which has
  already read the code, instead of asked again from the start.

  Added with 4aa81f5 (2026-10-17).
---
Your plan could not be used: {error}. Reply with the whole plan again, fixed, in the same JSON. You do not need to read anything more.
