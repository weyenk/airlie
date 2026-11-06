---
kind: steering
roles: [architect]
when: The architect wrote test files for a layer's package with the file tools (story.package_tools) and its turn ended with no package JSON (it ran out of steps, its thinking was cut on answer_after_cuts reads in a row, or it replied without the block); one short turn asks for the JSON alone.
governs:
  package.json: after-the-files
why: |
  The tool-written packages design (2026-11-06): 7 of the package tries from 2026-10-31 to 11-05 came back
  with no JSON block and 7 out of steps, and each was a whole new try. With the tests already written to
  files, the missing part is the JSON object, a short answer, not a rewrite.
---
Your test files are written. Reply now with the package JSON object only, in one ```json block, as the contract above asks: no test code and no more files.
