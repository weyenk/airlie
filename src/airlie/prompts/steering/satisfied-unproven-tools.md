---
kind: steering
roles: [architect]
when: The architect replied that a layer is already satisfied, its package written with the file tools (story.package_tools), and a test it named does not exist or does not pass on the layer's base, or a layer it named was not built below; said after the findings, closing the retry.
governs:
  package.fix: in-place
yields_to: [satisfied-unproven, package-fix]
why: |
  `satisfied-unproven` for a package written with the file tools (the tool-written packages design,
  2026-11-06): the other answer is the layer's tests written as files and the package JSON, not a reply
  holding every test file.
---
Either reply satisfied again with an earlier layer built below this one or tests that pass on the base as it is now (path::name), or write this layer's package: its test files with write_file and edit_file, then the JSON object. Do not write the feature's code.
