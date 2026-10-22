---
kind: rule
roles: [tester, coder]
fields: [files, noun]
governs:
  tests.location: named-file
why: |
  Where the coder puts the layer's tests: the file(s) the layer names, else a new file for the feature
  (AIR-28, 2026-10-18: a brief naming one file and the rule asking for a NEW one held the coder in a
  21-step argument with itself).

  This is the rule that wins: a layer whose scope or brief names a test file gets it instead of
  brief-layer-plan-tests-new-file (d7829af, PR 57, 2026-10-18: the build brief's new-test-file rule yields
  to a layer that names a test file). `files` is the named paths joined with " and ", `noun` is "file" or
  "files".
---
Put new tests in {files}, the {noun} this layer names.
