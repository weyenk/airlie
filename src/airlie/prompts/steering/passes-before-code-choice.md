---
kind: steering
roles: [tester]
when: The pre-code check found a test passing before the code; said under that finding in the tester's retry, indented two spaces by the code.
governs:
  tests.fail-first: required
  tests.unchanged: none
why: |
  Said under each test that passes before the code: delete it if it checks something the brief keeps as it
  is, else make it fail now. Added 2026-10-23 (AIR-9 layer 05: the brief said some notes stay role-less, the
  tester wrote a test of that, it passed before the code and the layer blocked, as "make it fail" gave no way
  out). The code puts the two spaces of indent before it; the file holds the sentence alone.
---
if it checks something the brief keeps unchanged, delete it (edit_file with the whole test as old and nothing as new); else make it fail now.
