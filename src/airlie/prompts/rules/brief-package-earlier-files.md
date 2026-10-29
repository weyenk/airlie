---
kind: rule
roles: [architect]
fields: [files]
governs:
  package.files: earlier-layers
why: |
  Packages are drafted up front at the story's base (2026-10-26), so a later layer's draft does not see the
  files the layers before it create. Roman reliability run 1 (2026-10-28): layer 02's draft created
  tests/test_roman.py, which layer 01 had made, and came back as file_exists and covers_missing.
---
## Files the layers before this one create
These are not on the base you read, but they exist when this layer is built: {files}. A file among them that this layer touches is one it modifies (action "modify"), never creates; a test file among them keeps every test the earlier layer put in it, whole, with this layer's tests added.
