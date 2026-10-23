---
kind: steering
roles: [tester, coder]
when: A role with write tools, in a run that must act, replies with text (or nothing) before any write of its own has landed; said once.
governs:
  writing: now
why: |
  What a role with write tools is told, once, when its reply is text (or empty) and no write of its own has
  landed yet (AIR-7 layer 02, 2026-10-18: a fix round answered the reviewer's findings in 2,186 tokens of prose
  and ended; layer 03: a fix round's one edit missed its anchor and the next reply was prose, taken as done;
  AIR-9 layer 02, 2026-10-21: a tester's retry read for six steps, then replied with nothing, taken as done).
---
Your reply called no tool, and nothing has been changed: the task is a change to make, not a question to answer. Make the change with the tools (write_file, edit_file), run run_tests, then stop with a few sentences on what you changed.
