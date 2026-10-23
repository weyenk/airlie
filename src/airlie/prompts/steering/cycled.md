---
kind: steering
roles: [any]
fields: [p, n, tool]
when: A role with nudge_stuck goes round the same cycle of up to MAX_CYCLE calls repeat_limit times with the same results; said once.
governs:
  approach: different
why: |
  REPEATED for a cycle of calls rather than one call (AIR-9 layer 02, 2026-10-21: a follow-up read the
  same three files in turn for 16 steps, and a guard that looked for one call repeated never fired).
  (REPEATED is now the steering text `repeated`.)
---
You have made the same {p} calls over and over, {n} times in a row, with the same results ({tool}). Going round them again changes nothing. Do something different: act on what you know, or give your answer.
