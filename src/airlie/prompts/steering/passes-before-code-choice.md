---
kind: steering
roles: [architect]
when: The package's red check found a test passing before the code; said under that finding in the architect's package feedback, indented two spaces by the code.
governs:
  tests.fail-first: required
  tests.unchanged: guard
why: |
  Said under each test that passes before the code. Added 2026-10-23 for the tester (AIR-9 layer 05: the brief
  said some notes stay role-less, the tester wrote a test of that, it passed before the code and the layer
  blocked, as "make it fail" gave no way out), when its way out was to delete the test with edit_file.
  Rewritten for the architect 2026-11-06: since P1 the architect writes the tests and replies with a package,
  not edit_file, and a criterion already true on main (AIR-59; AIR-61 layer 02, an unsigned DELETE already
  answered 401) could only be retried until the reply overflowed. The test of such a criterion is a regression
  guard. The code puts the two spaces of indent before it; the file holds the sentence alone.
---
if what it checks already holds on the layer's base, keep it and list it in the package's "guards" with one line naming what already makes it hold (and when every criterion of the layer already holds, reply {{"type": "satisfied", ...}} instead); else make it check the change this layer makes, so it fails now.
