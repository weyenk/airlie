---
kind: steering
roles: [architect]
when: A layer's package written with the file tools (story.package_tools) failed the readiness check and the architect is asked for it again; it closes the retry, after the findings and `package-files-placed`.
governs:
  package.fix: in-place
yields_to: [package-fix, satisfied-unproven]
why: |
  `package-fix` for a package written with the file tools (the tool-written packages design, 2026-11-06).
  There the reply was the whole package again, since the pipeline wrote the tests from the reply alone; here
  the tests are files in the checkout, so the fix is an edit to them and the reply is the JSON object. The
  rule that a test must fail for the right reason, or be a regression guard, is the same.
---
Fix these findings in the test files with edit_file, then reply with the package JSON object again, changed where the fixes change it. A test that passes before the code does not test this layer: make it fail for the right reason, mark it a regression guard when what it checks already holds on the base, or remove it if this layer added it (a test that was on the base stays). Do not write the feature's code.
