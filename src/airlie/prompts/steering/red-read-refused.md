---
kind: steering
roles: [coder]
when: A role with think_when_red calls read_file on a file it wrote or edited in this run while its last run_tests run failed; the call does not run.
governs:
  fix.when-red: think-then-one-edit
why: |
  Roman, 2026-10-28: told in words not to (red-fix), the red coder still read back what it had written,
  each step thinking to its budget. While red, a read of a file it wrote does not run; other reads and a
  shell probe do (refusing them all left it unable to see which input a looped test failed on).
---
This is a file you wrote in this conversation, so it was not read again: your write and edits above are what it holds. Work out the cause from the failing output, fix it with edit_file, then run run_tests.
