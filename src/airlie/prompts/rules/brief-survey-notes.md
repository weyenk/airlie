---
kind: rule
roles: [architect]
governs:
  notes.form: bullets
why: |
  What the survey's notes hold. No docstring in the code; added with the survey (ed0402e, 2026-10-17).
---
Notes, as short bullets with path:line where you can: (1) the files and symbols the change touches or must follow, (2) where the tests for them live and how they are named, (3) the conventions and registries a new name must be added to (config schema, tool lists, validators), (4) anything that could make a test pass for the wrong reason before the change.
