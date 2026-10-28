---
kind: steering
roles: [coder]
when: A role with think_when_red calls a tool other than write_file, edit_file or run_tests while its last run_tests run failed; the call does not run.
governs:
  fix.when-red: think-then-one-edit
why: |
  Roman, 2026-10-28: told in words not to (red-fix), the red coder still read, grepped, ran git and probed
  its module in the shell, each step thinking to its budget for ~90 seconds. While red its reads do not run.
---
Your tests are red, so reading is off until they pass: this call did not run. Your writes and edits above are the code as it is now, and the failing output above is what it does. Work out the cause in your thinking, fix it with edit_file (or write_file), then run run_tests.
