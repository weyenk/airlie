---
kind: steering
roles: [architect]
when: The architect replied that a layer is already satisfied, and a test it named does not exist or does not pass on the layer's base, or a layer it named was not built below; said after the findings, closing the retry.
governs:
  package.fix: whole-reply
why: |
  Review of PR #244 (2026-11-06): the satisfied reply must not be an escape hatch from a hard layer, so airlie
  runs the tests it names on the layer's base and takes it only when every one passes. What did not hold goes
  back here, as a package's findings do, and the layer is either proven satisfied or built.
---
Either reply satisfied again with an earlier layer built below this one or tests that pass on the base as it is now (path::name), or write this layer's whole package: every test file in full and the JSON object. Do not write the feature's code.
