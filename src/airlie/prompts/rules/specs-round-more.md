---
kind: rule
roles: [product]
fields: [rounds_left]
governs:
  specs.round.next: more-asks-or-the-epics
why: |
  Ends the follow-up that hands a round of consultation answers back to product (P4, 2026-10-25). Product
  may ask once more or reply with the epics; {rounds_left} is what story.specs.max_rounds leaves.
---
Reply with the next round of asks if an answer raised something that changes the epics ({rounds_left} round(s) left), or with the epics.
