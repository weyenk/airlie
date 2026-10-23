---
kind: steering
roles: [tester]
when: After the build, the whole gate fails only in the tester's locked tests; the tester is sent back once, with the failing tests, what the coder said and the reviewer's blocking findings after this.
governs:
  gate-failure.fix: tests
  tests.criteria: each
why: |
  What the tester is told when the gate fails only in its locked tests (AIR-9 layer 02, 2026-10-21: one of
  them unpacked one comment from a card that had two, and only the tester could change it).

  Added with ab0ee7c (2026-10-21).
---
The coder has built the layer, and the whole gate now fails only in your tests, which the coder cannot change. Check each failing test against the brief. If it is wrong in itself (how it sets up, unpacks or reads its data, a name or a signature the brief does not fix), fix that mistake in the test and nothing else: keep every check of what the layer must do, change no file but your tests, and do not touch the code. airlie checks again that each of your tests still fails without the layer's code, and undoes a change that does not. If your tests are right and the code is wrong, change nothing and say why. Run run_tests, then stop.
