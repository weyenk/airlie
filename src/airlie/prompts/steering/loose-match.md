---
kind: steering
roles: [tester]
when: A test the pre-code check found passing before the code has a `match=` pattern in its source; said under that finding in the tester's retry, indented two spaces by the code.
why: |
  Said under a test that passes before the code and matches an error's text: the usual cause.

  Added with df256b4 (2026-10-17, AIR-5 layer 01: the coder fixed two of three vacuous validation tests and
  kept the third's regex, which an existing unknown-tool message also satisfies); the tester gets it since
  the tester writes the tests (1741541, 2026-10-19). The code puts the two spaces of indent before it; the
  file holds the sentence alone.
---
its `match=` pattern is probably satisfied by an error that exists without the layer's code (an unknown name is refused with a message that lists every valid name). Match a phrase only the new message contains, as the brief gives it.
