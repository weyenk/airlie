---
kind: steering
roles: [tester, coder]
fields: [k]
when: A read is served under the hold while reading is switched off; it is appended to that read's observation, after a blank line.
governs:
  reading: stop
why: |
  Appended to a read served under the hold.

  The hold, and why it serves any reads: READS_UNDER_HOLD in teams/native_team.py (AIR-31 layer 02,
  2026-10-19: two runs spent their last 25 steps on refused greps, never wrote).
---
({k} read(s) left while reading is switched off; then write)
