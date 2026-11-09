---
kind: steering
roles: [any]
fields: [step]
when: A call has the same tool and arguments as one at an earlier step of this run, and its result is the same; this is the call's observation in place of that result. Not for write_file, edit_file or run_tests.
governs:
  reading.repeat: never
why: |
  AIR-66 layer 01 (2026-11-09): the architect sent the identical grep three times in a row, each answered in
  full. The result is in the conversation already; sending it again only grows it.
---
The same call as step {step}, and its result has not changed: it is above. Go on from it.
