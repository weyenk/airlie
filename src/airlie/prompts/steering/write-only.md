---
kind: steering
roles: [any]
fields: [what, n, k]
when: A role told to write (write-now, or a missed write deadline) has still not written WRITE_ONLY_AFTER steps later, and its reading is switched off.
why: |
  What a role is told, once, when the nudge did not make it write and its read tools are taken away;
  `what` is nothing, no test or no code.
---
You still have written {what} after {n} steps, so reading is switched off: {k} more reads, then only write_file, edit_file and run_tests are available until you write. Make the change from what you have read.
