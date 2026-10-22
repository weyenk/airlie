---
kind: rule
roles: [tester, coder]
governs:
  tests.location: new-file
yields_to: [brief-layer-plan-tests-where]
why: |
  Where the coder puts the layer's tests: the file(s) the layer names, else a new file for the feature
  (AIR-28, 2026-10-18: a brief naming one file and the rule asking for a NEW one held the coder in a
  21-step argument with itself).

  Sent only when the layer names no test file; it gives way to brief-layer-plan-tests-where (d7829af, PR 57,
  2026-10-18). Added with ba3cb08 (2026-10-16, HDS-5 layer 01: Qwen3-Coder rewrote a 17,000-character test
  file in one write_file, was cut off at max_tokens and derailed into empty calls).
---
Put new tests in a NEW test file named for the feature (e.g. tests/unit/test_<feature>.py), not appended to a big existing one.
