---
kind: steering
roles: [any]
fields: [tool]
when: A role with write tools and nudge_stuck repeats again after being told (repeated or cycled); the tool it loops on is switched off until it writes.
governs:
  approach: different
  writing: now
why: |
  What a role with write tools (nudge_stuck) is told when it repeats again after REPEATED: the tool it loops
  on goes, not the run, until it writes. A third streak, of refusals, ends the run.
  (REPEATED is now the steering text `repeated`, or `cycled` for a cycle of calls.)
---
You made the same call again after being told ({tool}). It is switched off: only write_file, edit_file and run_tests are available until you write. Make the change now from what you know.
