---
kind: steering
roles: [any]
fields: [n]
when: A resumed conversation's history held the last run's end-of-budget messages, now dropped; it is added to the resumed run's first message.
why: |
  Added to the first message of a resumed conversation whose history held the last run's end-of-budget
  messages (AIR-7 layer 02 fix round, 2026-10-18: the coder read "You have used all 40 of your steps" in its
  history and answered the reviewer's findings with "I'm out of steps, but here is the complete fix").
---
This is a new run: you have {n} steps and every tool again.
