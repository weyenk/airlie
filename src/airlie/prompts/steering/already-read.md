---
kind: steering
roles: [coder, architect, tester, product]
fields: [step]
when: A role calls read_file with the same path and range it read at an earlier step of this run, and nothing has written that file since; the read does not run again.
governs:
  reading.repeat: never
why: |
  Roman A/B run 2 (2026-10-29): drafting the packages, the architect read tests/test_version.py six times in
  a row, each step thinking to its budget and carried over, until the repeat guard stopped the drafting as
  unusable. A file read once is in the conversation; reading it again only grows it.
---
You read this file at step {step}, and nothing has changed it since: its text is above, so it was not read again. Work from that text and go on to your answer.
