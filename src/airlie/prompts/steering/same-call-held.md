---
kind: steering
roles: [any]
fields: [step]
when: As same-call, while reading is switched off; the repeated call costs none of the reads left under the hold, and the role is told to write.
governs:
  reading.repeat: never
  writing: now
why: |
  AIR-66 layer 01 (2026-11-09): under the hold the architect spent all three of its held reads on the one
  grep, "(no matches)" each time, and never wrote.
---
The same call as step {step}, and its result has not changed: it is above. Reading is switched off: write now with write_file or edit_file.
