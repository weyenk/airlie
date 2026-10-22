---
kind: steering
roles: [architect]
when: The architect plans in rounds and a round's plan was usable but not final; this starts the next round, in the same conversation.
why: |
  What the architect is told to start each later round.

  Added with planning in rounds (ed0402e, 2026-10-17: the layers stage took about 43 minutes on AIR-5,
  the architect using all 20 of its steps reading code at ever slower steps); it asks for the plan's own
  `refine` verdict since 480f537 (2026-10-17).
---
That is your plan so far. Read only what you named in its `refine.check`, for a few more steps; then reply with the whole plan again in the same JSON, with `refine` filled in again. If nothing you read changes it, reply with the same plan and `worth_another_round: false`.
