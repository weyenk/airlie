---
kind: steering
roles: [product, architect, tester]
when: The checker, airlie's model-less participant in the story meeting, re-read the three parts after a turn that changed one and found problems in them; it is posted in the meeting before the next speaker.
governs:
  agreement.findings: fix-next-turn
why: |
  The mechanical half of the three amigos (P2 of the planning line, 2026-10-24). What a program can check is
  checked before any agent has to notice it: a criterion with no test, a layer that owns a criterion product
  dropped, a file the base lacks. AIR-22 (2026-10-23) lost a layer's forty minutes to a contradiction nobody
  had read against the other criterion; the findings come back in the meeting, to the owner of the part,
  not in a layer twenty steps deep. Only the owner changes a part, so a finding is not for anyone else to fix.
---
airlie checked the parts as they stand and found these problems. The owner of each part fixes it in their next turn; nobody else changes it.
